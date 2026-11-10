from django.db import migrations, models

from rest_framework.authtoken.digests import KEY_FIELD_MAX_LENGTH


class Migration(migrations.Migration):

    dependencies = [
        ('authtoken', '0004_alter_tokenproxy_options'),
    ]

    operations = [
        # Widening the column is required before digest mode can be
        # enabled because a stored digest is longer than the 40
        # character legacy plaintext key. This migration does not change
        # any data and is reversible while all rows still hold legacy
        # keys; once digests have been written, reverting is not
        # possible without truncating them.
        migrations.AlterField(
            model_name='token',
            name='key',
            field=models.CharField(
                max_length=KEY_FIELD_MAX_LENGTH, primary_key=True,
                serialize=False, verbose_name='Key'
            ),
        ),
    ]
