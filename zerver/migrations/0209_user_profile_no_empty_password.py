# Generated by Django 1.11.24 on 2019-10-16 22:48

from typing import Any, Set, Union

import orjson
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import migrations
from django.db.backends.postgresql.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.utils.timezone import now as timezone_now

from zerver.lib.cache import cache_delete, user_profile_by_api_key_cache_key
from zerver.lib.queue import queue_json_publish
from zerver.lib.utils import generate_api_key


def ensure_no_empty_passwords(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """With CVE-2019-18933, it was possible for certain users created
    using social login (e.g. Google/GitHub auth) to have the empty
    string as their password in the Aloha database, rather than
    Django's "unusable password" (i.e. no password at all).  This was a
    serious security issue for organizations with both password and
    Google/GitHub authentication enabled.

    Combined with the code changes to prevent new users from entering
    this buggy state, this migration sets the intended "no password"
    state for any users who are in this buggy state, as had been
    intended.

    While this bug was discovered by our own development team and we
    believe it hasn't been exploited in the wild, out of an abundance
    of caution, this migration also resets the personal API keys for
    all users where Aloha's database-level logging cannot **prove**
    that user's current personal API key was never accessed using this
    bug.

    There are a few ways this can be proven: (1) the user's password
    has never been changed and is not the empty string,
    or (2) the user's personal API key has changed since that user last
    changed their password (which is not ''). Both constitute proof
    because this bug cannot be used to gain the access required to change
    or reset a user's password.

    Resetting those API keys has the effect of logging many users out
    of the Aloha mobile and terminal apps unnecessarily (e.g. because
    the user changed their password at any point in the past, even
    though the user never was affected by the bug), but we're
    comfortable with that cost for ensuring that this bug is
    completely fixed.

    To avoid this inconvenience for self-hosted servers which don't
    even have EmailAuthBackend enabled, we skip resetting any API keys
    if the server doesn't have EmailAuthBackend configured.
    """

    UserProfile = apps.get_model("zerver", "UserProfile")
    RealmAuditLog = apps.get_model("zerver", "RealmAuditLog")

    # Because we're backporting this migration to the Aloha 2.0.x
    # series, we've given it migration number 0209, which is a
    # duplicate with an existing migration already merged into Aloha
    # main.  Migration 0247_realmauditlog_event_type_to_int.py
    # changes the format of RealmAuditLog.event_type, so we need the
    # following conditional block to determine what values to use when
    # searching for the relevant events in that log.
    event_type_class = RealmAuditLog._meta.get_field("event_type").get_internal_type()
    if event_type_class == "CharField":
        USER_PASSWORD_CHANGED: Union[int, str] = "user_password_changed"
        USER_API_KEY_CHANGED: Union[int, str] = "user_api_key_changed"
    else:
        USER_PASSWORD_CHANGED = 122
        USER_API_KEY_CHANGED = 127

    # First, we do some bulk queries to collect data we'll find useful
    # in the loop over all users below.

    # Users who changed their password at any time since account
    # creation.  These users could theoretically have started with an
    # empty password, but set a password later via the password reset
    # flow.  If their API key has changed since they changed their
    # password, we can prove their current API key cannot have been
    # exposed; we store those users in
    # password_change_user_ids_no_reset_needed.
    password_change_user_ids = set(
        RealmAuditLog.objects.filter(event_type=USER_PASSWORD_CHANGED).values_list(
            "modified_user_id", flat=True
        )
    )
    password_change_user_ids_api_key_reset_needed: Set[int] = set()
    password_change_user_ids_no_reset_needed: Set[int] = set()

    for user_id in password_change_user_ids:
        # Here, we check the timing for users who have changed
        # their password.

        # We check if the user changed their API key since their first password change.
        query = RealmAuditLog.objects.filter(
            modified_user=user_id,
            event_type__in=[USER_PASSWORD_CHANGED, USER_API_KEY_CHANGED],
        ).order_by("event_time")

        earliest_password_change = query.filter(event_type=USER_PASSWORD_CHANGED).first()
        # Since these users are in password_change_user_ids, this must not be None.
        assert earliest_password_change is not None

        latest_api_key_change = query.filter(event_type=USER_API_KEY_CHANGED).last()
        if latest_api_key_change is None:
            # This user has never changed their API key.  As a
            # result, even though it's very likely this user never
            # had an empty password, they have changed their
            # password, and we have no record of the password's
            # original hash, so we can't prove the user's API key
            # was never affected.  We schedule this user's API key
            # to be reset.
            password_change_user_ids_api_key_reset_needed.add(user_id)
        elif earliest_password_change.event_time <= latest_api_key_change.event_time:
            # This user has changed their password before
            # generating their current personal API key, so we can
            # prove their current personal API key could not have
            # been exposed by this bug.
            password_change_user_ids_no_reset_needed.add(user_id)
        else:
            password_change_user_ids_api_key_reset_needed.add(user_id)

    if password_change_user_ids_no_reset_needed and settings.PRODUCTION:
        # We record in this log file users whose current API key was
        # generated after a real password was set, so there's no need
        # to reset their API key, but because they've changed their
        # password, we don't know whether or not they originally had a
        # buggy password.
        #
        # In theory, this list can be recalculated using the above
        # algorithm modified to only look at events before the time
        # this migration was installed, but it's helpful to log it as well.
        with open("/var/log/zulip/0209_password_migration.log", "w") as log_file:
            line = "No reset needed, but changed password: {}\n"
            log_file.write(line.format(password_change_user_ids_no_reset_needed))

    AFFECTED_USER_TYPE_EMPTY_PASSWORD = "empty_password"
    AFFECTED_USER_TYPE_CHANGED_PASSWORD = "changed_password"
    MIGRATION_ID = "0209_user_profile_no_empty_password"

    def write_realm_audit_log_entry(
        user_profile: Any, event_time: Any, event_type: Any, affected_user_type: str
    ) -> None:
        RealmAuditLog.objects.create(
            realm=user_profile.realm,
            modified_user=user_profile,
            event_type=event_type,
            event_time=event_time,
            extra_data=orjson.dumps(
                {
                    "migration_id": MIGRATION_ID,
                    "affected_user_type": affected_user_type,
                }
            ).decode(),
        )

    # If Aloha's built-in password authentication is not enabled on
    # the server level, then we plan to skip resetting any users' API
    # keys, since the bug requires EmailAuthBackend.
    email_auth_enabled = "zproject.backends.EmailAuthBackend" in settings.AUTHENTICATION_BACKENDS

    # A quick note: This query could in theory exclude users with
    # is_active=False, is_bot=True, or realm__deactivated=True here to
    # accessing only active human users in non-deactivated realms.
    # But it's better to just be thorough; users can be reactivated,
    # and e.g. a server admin could manually edit the database to
    # change a bot into a human user if they really wanted to.  And
    # there's essentially no harm in rewriting state for a deactivated
    # account.
    for user_profile in UserProfile.objects.all():
        event_time = timezone_now()
        if check_password("", user_profile.password):
            # This user currently has the empty string as their password.

            # Change their password and record that we did so.
            user_profile.password = make_password(None)
            update_fields = ["password"]
            write_realm_audit_log_entry(
                user_profile, event_time, USER_PASSWORD_CHANGED, AFFECTED_USER_TYPE_EMPTY_PASSWORD
            )

            if email_auth_enabled and not user_profile.is_bot:
                # As explained above, if the built-in password authentication
                # is enabled, reset the API keys. We can skip bot accounts here,
                # because the `password` attribute on a bot user is useless.
                reset_user_api_key(user_profile)
                update_fields.append("api_key")

                event_time = timezone_now()
                write_realm_audit_log_entry(
                    user_profile,
                    event_time,
                    USER_API_KEY_CHANGED,
                    AFFECTED_USER_TYPE_EMPTY_PASSWORD,
                )

            user_profile.save(update_fields=update_fields)
            continue

        elif (
            email_auth_enabled and user_profile.id in password_change_user_ids_api_key_reset_needed
        ):
            # For these users, we just need to reset the API key.
            reset_user_api_key(user_profile)
            user_profile.save(update_fields=["api_key"])

            write_realm_audit_log_entry(
                user_profile, event_time, USER_API_KEY_CHANGED, AFFECTED_USER_TYPE_CHANGED_PASSWORD
            )


def reset_user_api_key(user_profile: Any) -> None:
    old_api_key = user_profile.api_key
    user_profile.api_key = generate_api_key()
    cache_delete(user_profile_by_api_key_cache_key(old_api_key))

    # Like with any API key change, we need to clear any server-side
    # state for sending push notifications to mobile app clients that
    # could have been registered with the old API key.  Fortunately,
    # we can just write to the queue processor that handles sending
    # those notices to the push notifications bouncer service.
    event = {"type": "clear_push_device_tokens", "user_profile_id": user_profile.id}
    queue_json_publish("deferred_work", event)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("zerver", "0208_add_realm_night_logo_fields"),
    ]

    operations = [
        migrations.RunPython(
            ensure_no_empty_passwords, reverse_code=migrations.RunPython.noop, elidable=True
        ),
    ]
