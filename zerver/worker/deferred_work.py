# Documented in https://zulip.readthedocs.io/en/latest/subsystems/queuing.html
import logging
import tempfile
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _
from django.utils.translation import override as override_language
from typing_extensions import override

from zerver.actions.message_flags import do_mark_stream_messages_as_read
from zerver.actions.message_send import internal_send_private_message
from zerver.actions.realm_export import notify_realm_export
from zerver.lib.export import export_realm_wrapper
from zerver.lib.push_notifications import clear_push_device_tokens
from zerver.lib.queue import queue_json_publish_rollback_unsafe, retry_event
from zerver.lib.remote_server import (
    PushNotificationBouncerRetryLaterError,
    send_server_data_to_push_bouncer,
)
from zerver.lib.soft_deactivation import reactivate_user_if_soft_deactivated
from zerver.lib.upload import handle_reupload_emojis_event
from zerver.models import Message, Realm, RealmAuditLog, RealmExport, Stream, UserMessage
from zerver.models.users import get_system_bot, get_user_profile_by_id
from zerver.worker.base import QueueProcessingWorker, assign_queue

logger = logging.getLogger(__name__)


@assign_queue("deferred_work")
class DeferredWorker(QueueProcessingWorker):
    """This queue processor is intended for cases where we want to trigger a
    potentially expensive, not urgent, job to be run on a separate
    thread from the Django worker that initiated it (E.g. so we that
    can provide a low-latency HTTP response or avoid risk of request
    timeouts for an operation that could in rare cases take minutes).
    """

    # Because these operations have no SLO, and can take minutes,
    # remove any processing timeouts
    MAX_CONSUME_SECONDS = None

    @override
    def consume(self, event: dict[str, Any]) -> None:
        start = time.time()
        if event["type"] == "mark_stream_messages_as_read":
            user_profile = get_user_profile_by_id(event["user_profile_id"])
            logger.info(
                "Marking messages as read for user %s, stream_recipient_ids %s",
                user_profile.id,
                event["stream_recipient_ids"],
            )

            for recipient_id in event["stream_recipient_ids"]:
                count = do_mark_stream_messages_as_read(user_profile, recipient_id)
                logger.info(
                    "Marked %s messages as read for user %s, stream_recipient_id %s",
                    count,
                    user_profile.id,
                    recipient_id,
                )
        elif event["type"] == "mark_stream_messages_as_read_for_everyone":
            logger.info(
                "Marking messages as read for all users, stream_recipient_id %s",
                event["stream_recipient_id"],
            )
            stream = Stream.objects.get(recipient_id=event["stream_recipient_id"])
            # This event is generated by the stream deactivation code path.
            batch_size = 50
            start_time = time.perf_counter()
            min_id = event.get("min_id", 0)
            total_messages = 0
            while True:
                with transaction.atomic(savepoint=False):
                    messages = list(
                        Message.objects.filter(
                            # Uses index: zerver_message_realm_recipient_id
                            realm_id=stream.realm_id,
                            recipient_id=event["stream_recipient_id"],
                            id__gt=min_id,
                        )
                        .order_by("id")[:batch_size]
                        .values_list("id", flat=True)
                    )
                    UserMessage.select_for_update_query().filter(message__in=messages).extra(  # noqa: S610
                        where=[UserMessage.where_unread()]
                    ).update(flags=F("flags").bitor(UserMessage.flags.read))
                total_messages += len(messages)
                if len(messages) < batch_size:
                    break
                min_id = messages[-1]
                if time.perf_counter() - start_time > 30:
                    # This task may take a _very_ long time to
                    # complete, if we have a large number of messages
                    # to mark as read.  If we have taken more than
                    # 30s, we re-push the task onto the tail of the
                    # queue, to allow other deferred work to complete;
                    # this task is extremely low priority.
                    queue_json_publish_rollback_unsafe("deferred_work", {**event, "min_id": min_id})
                    break
            logger.info(
                "Marked %s messages as read for all users, stream_recipient_id %s",
                total_messages,
                event["stream_recipient_id"],
            )
        elif event["type"] == "clear_push_device_tokens":
            logger.info(
                "Clearing push device tokens for user_profile_id %s",
                event["user_profile_id"],
            )
            try:
                clear_push_device_tokens(event["user_profile_id"])
            except PushNotificationBouncerRetryLaterError:

                def failure_processor(event: dict[str, Any]) -> None:
                    logger.warning(
                        "Maximum retries exceeded for trigger:%s event:clear_push_device_tokens",
                        event["user_profile_id"],
                    )

                retry_event(self.queue_name, event, failure_processor)
        elif event["type"] == "realm_export":
            output_dir = tempfile.mkdtemp(prefix="zulip-export-")
            user_profile = get_user_profile_by_id(event["user_profile_id"])
            realm = user_profile.realm
            export_event = None

            if "realm_export_id" in event:
                export_row = RealmExport.objects.get(id=event["realm_export_id"])
            else:
                # Handle existing events in the queue before we switched to RealmExport model.
                export_event = RealmAuditLog.objects.get(id=event["id"])
                extra_data = export_event.extra_data

                if extra_data.get("export_row_id") is not None:
                    export_row = RealmExport.objects.get(id=extra_data["export_row_id"])
                else:
                    export_row = RealmExport.objects.create(
                        realm=realm,
                        type=RealmExport.EXPORT_PUBLIC,
                        acting_user=user_profile,
                        status=RealmExport.REQUESTED,
                        date_requested=event["time"],
                    )
                    export_event.extra_data = {"export_row_id": export_row.id}
                    export_event.save(update_fields=["extra_data"])

            if export_row.status != RealmExport.REQUESTED:
                logger.error(
                    "Marking export for realm %s as failed due to retry -- possible OOM during export?",
                    realm.string_id,
                )
                export_row.status = RealmExport.FAILED
                export_row.date_failed = timezone_now()
                export_row.save(update_fields=["status", "date_failed"])
                notify_realm_export(realm)
                return

            logger.info(
                "Starting realm export for realm %s into %s, initiated by user_profile_id %s",
                realm.string_id,
                output_dir,
                user_profile.id,
            )

            try:
                export_realm_wrapper(
                    export_row=export_row,
                    output_dir=output_dir,
                    threads=1 if self.threaded else 6,
                    upload=True,
                )
            except Exception:
                logging.exception(
                    "Data export for %s failed after %s",
                    realm.string_id,
                    time.time() - start,
                    stack_info=True,
                )
                notify_realm_export(realm)
                return

            # We create RealmAuditLog entry in 'export_realm_wrapper'.
            # Delete the old entry created before we switched to RealmExport model.
            if export_event:
                export_event.delete()

            # Send a direct message notification letting the user who
            # triggered the export know the export finished.
            with override_language(user_profile.default_language):
                content = _(
                    "Your data export is complete. [View and download exports]({export_settings_link})."
                ).format(export_settings_link="/#organization/data-exports-admin")
            internal_send_private_message(
                sender=get_system_bot(settings.NOTIFICATION_BOT, realm.id),
                recipient_user=user_profile,
                content=content,
            )

            # For future frontend use, also notify administrator
            # clients that the export happened.
            notify_realm_export(realm)
            logging.info(
                "Completed data export for %s in %s",
                realm.string_id,
                time.time() - start,
            )
        elif event["type"] == "reupload_realm_emoji":
            # This is a special event queued by the migration for reuploading emojis.
            # We don't want to run the necessary code in the actual migration, so it simply
            # queues the necessary event, and the actual work is done here in the queue worker.
            realm = Realm.objects.get(id=event["realm_id"])
            logger.info("Processing reupload_realm_emoji event for realm %s", realm.id)
            handle_reupload_emojis_event(realm, logger)
        elif event["type"] == "soft_reactivate":
            logger.info(
                "Starting soft reactivation for user_profile_id %s",
                event["user_profile_id"],
            )
            user_profile = get_user_profile_by_id(event["user_profile_id"])
            reactivate_user_if_soft_deactivated(user_profile)
        elif event["type"] == "push_bouncer_update_for_realm":
            # In the future we may use the realm_id to send only that single realm's info.
            realm_id = event["realm_id"]
            logger.info("Updating push bouncer with metadata on behalf of realm %s", realm_id)
            send_server_data_to_push_bouncer(consider_usage_statistics=False)

        end = time.time()
        logger.info(
            "deferred_work processed %s event (%dms)",
            event["type"],
            (end - start) * 1000,
        )
