from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("signals", "0178_signalscoutconfig_autovacuum_scale_factor")]

    operations = [
        migrations.AddField(
            model_name="signalteamconfig",
            name="onboarding",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
