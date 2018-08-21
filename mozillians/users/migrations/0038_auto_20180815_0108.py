# -*- coding: utf-8 -*-
# Generated by Django 1.11.15 on 2018-08-15 08:08
from __future__ import unicode_literals

from django.db import migrations
from django.conf import settings
from django.utils.timezone import now


def add_missing_employee_vouches(apps, schema_editor):
    UserProfile = apps.get_model('users', 'UserProfile')
    IdpProfile = apps.get_model('users', 'IdpProfile')

    for profile in UserProfile.objects.all():
        emails = [idp.email for idp in IdpProfile.objects.filter(profile=profile)]

        email_exists = any([email for email in set(emails)
                            if email.split('@')[1] in settings.AUTO_VOUCH_DOMAINS])
        if email_exists and not profile.vouches_received.filter(
                description=settings.AUTO_VOUCH_REASON, autovouch=True).exists():

            profile.vouches_received.create(
                voucher=None,
                date=now(),
                description=settings.AUTO_VOUCH_REASON,
                autovouch=True
            )

            vouches = profile.vouches_received.all().count()
            UserProfile.objects.filter(pk=profile.pk).update(
                is_vouched=vouches > 0,
                can_vouch=vouches >= settings.CAN_VOUCH_THRESHOLD
            )


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0037_auto_20180720_0305'),
    ]

    operations = [
        migrations.RunPython(add_missing_employee_vouches, backwards),
    ]
