from django.db import migrations

from posthog.migration_helpers import SafeDropTable


class Migration(migrations.Migration):
    """Drop the table 1398 took out of Django state.

    1398 removed UserProductList from state and dropped its foreign keys, and kept the table so
    pods on the old release could still read it through a rolling deploy. Nothing reads it now.
    """

    dependencies = [
        ("posthog", "1399_alter_user_ui_configuration_help_text"),
    ]

    operations = [
        SafeDropTable("posthog_userproductlist"),
    ]
