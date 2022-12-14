from typing import Any, Iterable

from django.core.management.base import CommandParser

from zerver.lib.management import AlohaBaseCommand
from zerver.lib.message import maybe_update_first_visible_message_id
from zerver.models import Realm


class Command(AlohaBaseCommand):
    help = """Calculate the value of first visible message ID and store it in cache"""

    def add_arguments(self, parser: CommandParser) -> None:
        self.add_realm_args(parser)
        parser.add_argument(
            "--lookback-hours",
            type=int,
            help="Period a bit larger than that of the cron job that runs "
            "this command so that the lookback periods are sure to overlap.",
            required=True,
        )

    def handle(self, *args: Any, **options: Any) -> None:
        target_realm = self.get_realm(options)

        if target_realm is None:
            realms: Iterable[Realm] = Realm.objects.all()
        else:
            realms = [target_realm]

        for realm in realms:
            maybe_update_first_visible_message_id(realm, options["lookback_hours"])
