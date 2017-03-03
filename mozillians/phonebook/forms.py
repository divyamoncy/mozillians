import re
from cStringIO import StringIO
from datetime import datetime

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.forms.models import BaseInlineFormSet, inlineformset_factory
from django.forms.widgets import RadioSelect
from django.utils.translation import ugettext as _, ugettext_lazy as _lazy

import django_filters
import happyforms
from dal import autocomplete
from nocaptcha_recaptcha.fields import NoReCaptchaField
from PIL import Image

from mozillians.api.models import APIv2App
from mozillians.common.urlresolvers import reverse
from mozillians.phonebook.models import Invite
from mozillians.phonebook.validators import validate_username
from mozillians.phonebook.widgets import MonthYearWidget
from mozillians.users import get_languages_for_locale
from mozillians.users.models import AbuseReport, ExternalAccount, Language, UserProfile


REGEX_NUMERIC = re.compile('\d+', re.IGNORECASE)


class ExternalAccountForm(happyforms.ModelForm):
    class Meta:
        model = ExternalAccount
        fields = ['type', 'identifier', 'privacy']

    def clean(self):
        cleaned_data = super(ExternalAccountForm, self).clean()
        identifier = cleaned_data.get('identifier')
        account_type = cleaned_data.get('type')

        if account_type and identifier:
            # If the Account expects an identifier and user provided a
            # full URL, try to extract the identifier from the URL.
            url = ExternalAccount.ACCOUNT_TYPES[account_type].get('url')
            if url and identifier.startswith('http'):
                url_pattern_re = url.replace('{identifier}', '(.+)')
                identifier = identifier.rstrip('/')
                url_pattern_re = url_pattern_re.rstrip('/')
                match = re.match(url_pattern_re, identifier)
                if match:
                    identifier = match.groups()[0]

            validator = ExternalAccount.ACCOUNT_TYPES[account_type].get('validator')
            if validator:
                identifier = validator(identifier)

            cleaned_data['identifier'] = identifier

        return cleaned_data


AccountsFormset = inlineformset_factory(UserProfile, ExternalAccount,
                                        form=ExternalAccountForm, extra=1)


class AlternateEmailForm(happyforms.ModelForm):
    class Meta:
        model = ExternalAccount
        fields = ['privacy']


AlternateEmailFormset = inlineformset_factory(UserProfile, ExternalAccount,
                                              form=AlternateEmailForm, extra=0)


class EmailPrivacyForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['privacy_email']


class SearchForm(happyforms.Form):
    q = forms.CharField(required=False, max_length=140)
    limit = forms.IntegerField(
        widget=forms.HiddenInput, required=False, min_value=1,
        max_value=settings.ITEMS_PER_PAGE)
    include_non_vouched = forms.BooleanField(
        label=_lazy(u'Include non-vouched'), required=False)

    def clean_limit(self):
        limit = self.cleaned_data['limit'] or settings.ITEMS_PER_PAGE
        return limit


def filter_vouched(qs, choice):
    if choice == SearchFilter.CHOICE_ONLY_VOUCHED:
        return qs.filter(is_vouched=True)
    elif choice == SearchFilter.CHOICE_ONLY_UNVOUCHED:
        return qs.filter(is_vouched=False)
    return qs


class SearchFilter(django_filters.FilterSet):
    CHOICE_ONLY_VOUCHED = 'yes'
    CHOICE_ONLY_UNVOUCHED = 'no'
    CHOICE_ALL = 'all'

    CHOICES = (
        (CHOICE_ONLY_VOUCHED, _lazy('Vouched')),
        (CHOICE_ONLY_UNVOUCHED, _lazy('Unvouched')),
        (CHOICE_ALL, _lazy('All')),
    )

    vouched = django_filters.ChoiceFilter(
        name='vouched', label=_lazy(u'Display only'), required=False,
        choices=CHOICES, action=filter_vouched)

    class Meta:
        model = UserProfile
        fields = ['vouched', 'skills', 'groups', 'timezone']

    def __init__(self, *args, **kwargs):
        super(SearchFilter, self).__init__(*args, **kwargs)
        self.filters['timezone'].field.choices.insert(0, ('', _lazy(u'All timezones')))


class UserForm(happyforms.ModelForm):
    """Instead of just inhereting form a UserProfile model form, this
    base class allows us to also abstract over methods that have to do
    with the User object that need to exist in both Registration and
    Profile.

    """
    username = forms.CharField(label=_lazy(u'Username'))

    class Meta:
        model = User
        fields = ['username']

    def clean_username(self):
        username = self.cleaned_data['username']
        if not username:
            return self.instance.username

        # Don't be jacking somebody's username
        # This causes a potential race condition however the worst that can
        # happen is bad UI.
        if (User.objects.filter(username=username).
                exclude(pk=self.instance.id).exists()):
            raise forms.ValidationError(_(u'This username is in use. Please try'
                                          u' another.'))

        # No funky characters in username.
        if not re.match(r'^[\w.@+-]+$', username):
            raise forms.ValidationError(_(u'Please use only alphanumeric'
                                          u' characters'))

        if not validate_username(username):
            raise forms.ValidationError(_(u'This username is not allowed, '
                                          u'please choose another.'))
        return username


class BasicInformationForm(happyforms.ModelForm):
    photo = forms.ImageField(label=_lazy(u'Profile Photo'), required=False)
    photo_delete = forms.BooleanField(label=_lazy(u'Remove Profile Photo'),
                                      required=False)

    class Meta:
        model = UserProfile
        fields = ('photo', 'privacy_photo', 'full_name', 'privacy_full_name',
                  'full_name_local', 'privacy_full_name_local', 'bio', 'privacy_bio',)
        widgets = {'bio': forms.Textarea()}

    def clean_photo(self):
        """Clean possible bad Image data.

        Try to load EXIF data from image. If that fails, remove EXIF
        data by re-saving the image. Related bug 919736.

        """
        photo = self.cleaned_data['photo']
        if photo and isinstance(photo, UploadedFile):
            image = Image.open(photo.file)
            try:
                image._get_exif()
            except (AttributeError, IOError, KeyError, IndexError):
                cleaned_photo = StringIO()
                if image.mode != 'RGB':
                    image = image.convert('RGB')
                image.save(cleaned_photo, format='JPEG', quality=95)
                photo.file = cleaned_photo
                photo.size = cleaned_photo.tell()
        return photo


class SkillsForm(happyforms.ModelForm):

    def __init__(self, *args, **kwargs):
        """Override init method."""
        super(SkillsForm, self).__init__(*args, **kwargs)
        # Override the url to pass along the locale.
        # This is needed in order to post to the correct url through ajax
        self.fields['skills'].widget.url = reverse('groups:skills-autocomplete')

    class Meta:
        model = UserProfile
        fields = ('privacy_skills', 'skills',)
        widgets = {
            'skills': autocomplete.ModelSelect2Multiple(
                url='groups:skills-autocomplete',
                attrs={
                    'data-placeholder': (u'Start typing to add a skill (example: Python, '
                                         u'javascript, Graphic Design, User Research)'),
                    'data-minimum-input-length': 3
                }
            )
        }


class LanguagesPrivacyForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('privacy_languages',)


class LocationForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('timezone', 'privacy_timezone',)


class ContributionForm(happyforms.ModelForm):
    date_mozillian = forms.DateField(
        required=False,
        label=_lazy(u'When did you get involved with Mozilla?'),
        widget=MonthYearWidget(years=range(1998, datetime.today().year + 1),
                               required=False))

    class Meta:
        model = UserProfile
        fields = ('title', 'privacy_title',
                  'date_mozillian', 'privacy_date_mozillian',
                  'story_link', 'privacy_story_link',)


class TshirtForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('tshirt', 'privacy_tshirt',)


class GroupsPrivacyForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('privacy_groups',)


class IRCForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('ircname', 'privacy_ircname',)


class DeveloperForm(happyforms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ('allows_community_sites', 'allows_mozilla_sites',)


class BaseLanguageFormSet(BaseInlineFormSet):

    def __init__(self, *args, **kwargs):
        self.locale = kwargs.pop('locale', 'en')
        super(BaseLanguageFormSet, self).__init__(*args, **kwargs)

    def add_fields(self, form, index):
        super(BaseLanguageFormSet, self).add_fields(form, index)
        choices = [('', '---------')] + get_languages_for_locale(self.locale)
        form.fields['code'].choices = choices

    class Meta:
        models = Language
        fields = ['code']


LanguagesFormset = inlineformset_factory(UserProfile, Language,
                                         formset=BaseLanguageFormSet,
                                         extra=1, fields='__all__')


class EmailForm(happyforms.Form):
    email = forms.EmailField(label=_lazy(u'Email'))

    def clean_email(self):
        email = self.cleaned_data['email']
        if (User.objects.exclude(pk=self.initial['user_id']).filter(email=email).exists()):
            raise forms.ValidationError(_(u'Email is currently associated with another user.'))
        return email

    def email_changed(self):
        return self.cleaned_data['email'] != self.initial['email']


class RegisterForm(BasicInformationForm):
    optin = forms.BooleanField(
        widget=forms.CheckboxInput(attrs={'class': 'checkbox'}),
        required=True)
    captcha = NoReCaptchaField()

    class Meta:
        model = UserProfile
        fields = ('photo', 'full_name', 'timezone', 'privacy_photo', 'privacy_full_name', 'optin',
                  'privacy_timezone',)


class VouchForm(happyforms.Form):
    """Vouching is captured via a user's id and a description of the reason for vouching."""
    description = forms.CharField(
        label=_lazy(u'Provide a reason for vouching with relevant links'),
        widget=forms.Textarea(attrs={'rows': 10, 'cols': 20, 'maxlength': 500}),
        max_length=500,
        error_messages={'required': _(u'You must enter a reason for vouching for this person.')}
    )


class InviteForm(happyforms.ModelForm):
    message = forms.CharField(
        label=_lazy(u'Personal message to be included in the invite email'),
        required=False, widget=forms.Textarea(),
    )
    recipient = forms.EmailField(label=_lazy(u"Recipient's email"))

    def clean_recipient(self):
        recipient = self.cleaned_data['recipient']
        if User.objects.filter(email=recipient,
                               userprofile__is_vouched=True).exists():
            raise forms.ValidationError(
                _(u'You cannot invite someone who has already been vouched.'))
        return recipient

    class Meta:
        model = Invite
        fields = ['recipient']


class APIKeyRequestForm(happyforms.ModelForm):

    class Meta:
        model = APIv2App
        fields = ('name', 'description', 'url',)


class AbuseReportForm(happyforms.ModelForm):

    class Meta:
        model = AbuseReport
        fields = ('type',)
        widgets = {
            'type': RadioSelect
        }
        labels = {
            'type': _(u'What would you like to report?')
        }
