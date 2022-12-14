from typing import Any

from django.core.management.base import CommandParser

from zerver.actions.streams import bulk_remove_subscriptions
from zerver.lib.management import AlohaBaseCommand
from zerver.models import get_stream


class Command(AlohaBaseCommand):
    help = """Remove some or all users in a realm from a stream."""

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("-s", "--stream", required=True, help="A stream name.")

        self.add_realm_args(parser, required=True)
        self.add_user_list_args(
            parser, all_users_help="Remove all users in realm from this stream."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        realm = self.get_realm(options)
        assert realm is not None  # Should be ensured by parser
        user_profiles = self.get_users(options, realm)
        stream_name = options["stream"].strip()
        stream = get_stream(stream_name, realm)

        result = bulk_remove_subscriptions(realm, user_profiles, [stream], acting_user=None)
        not_subscribed = result[1]
        not_subscribed_users = {tup[0] for tup in not_subscribed}

        for user_profile in user_profiles:
            if user_profile in not_subscribed_users:
                print(f"{user_profile.delivery_email} was not subscribed")
            else:
                print(f"Removed {user_profile.delivery_email} from {stream_name}")
