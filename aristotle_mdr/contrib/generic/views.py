from django import forms
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.db import transaction
from django.forms.models import modelformset_factory
from django.http import HttpResponse, Http404, HttpResponseRedirect
from django.shortcuts import redirect, get_object_or_404
from django.utils.translation import ugettext_lazy as _
from django.views.generic import FormView, ListView, TemplateView

import autocomplete_light

from aristotle_mdr.utils import get_concepts_for_apps
from aristotle_mdr.models import _concept
from aristotle_mdr.perms import user_can_edit
from aristotle_mdr.utils import construct_change_message

import reversion


class GenericAlterManyToSomethingFormView(FormView):
    model_base = None
    model_to_add = None
    model_base_field = None
    form_title = None
    form_submit_text = _('Save')

    def dispatch(self, request, *args, **kwargs):
        self.item = get_object_or_404(self.model_base, pk=self.kwargs['iid'])
        if not (self.item and user_can_edit(request.user, self.item)):
            if request.user.is_anonymous():
                return redirect(reverse('friendly_login') + '?next=%s' % request.path)
            else:
                raise PermissionDenied
        return super(GenericAlterManyToSomethingFormView, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(GenericAlterManyToSomethingFormView, self).get_context_data(**kwargs)
        context['model_to_add'] = self.model_to_add
        context['model_base'] = self.model_base
        context['item'] = self.item
        context['form_title'] = self.form_title or _('Add child item')
        context['submit_url'] = self.request.get_full_path()
        context['form_submit_text'] = self.form_submit_text
        return context

    def get_success_url(self):
        return self.item.get_absolute_url()


class GenericAlterManyToManyView(GenericAlterManyToSomethingFormView):
    """
    A view that provides a framework for altering ManyToMany relationships from
    one 'base' object to many others.

    The URL pattern must pass a kwarg with the name `iid` that is the object from the
    `model_base` to use as the main link for the many to many relation.

    * `model_base` - mandatory - The model with the instance to be altered
    * `model_to_add` - mandatory - The model that has instances we will link to the base.
    * `template_name`
        - optional - The template used to display the form.
        - default - "aristotle_mdr/generic/actions/alter_many_to_many.html"
    * `model_base_field` - mandatory - the field name that goes from the `model_base` to the `model_to_add`.
    * `form_title` - Title for the form

    For example: I fwe have a mnay to many relationship from `DataElement`s to
    `Dataset`s, to alter the `DataElement`s attached to a `Dataset`, `Dataset` is the
    `base_model` and `model_to_add` is `DataElement`.
    """

    template_name = "aristotle_mdr/generic/actions/alter_many_to_many.html"

    def get_context_data(self, **kwargs):
        context = super(GenericAlterManyToManyView, self).get_context_data(**kwargs)
        return context

    def get_form_class(self):
        class M2MForm(forms.Form):
            items_to_add = forms.ModelMultipleChoiceField(
                queryset=self.model_to_add.objects.visible(self.request.user),
                label="Attach",
                required=False,
                widget=autocomplete_light.MultipleChoiceWidget(
                    self.model_to_add.get_autocomplete_name()
                ),
            )
        return M2MForm

    def get_initial(self):
        return {
            'items_to_add': getattr(self.item, self.model_base_field).all()
        }

    def post(self, request, *args, **kwargs):
        """
        Handles POST requests, instantiating a form instance with the passed
        POST variables and then checked for validity.
        """
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        self.item.__setattr__(self.model_base_field, form.cleaned_data['items_to_add'])
        self.item.save()
        return HttpResponseRedirect(self.get_success_url())


class GenericAlterOneToManyView(GenericAlterManyToSomethingFormView):
    """
    A view that provides a framework for altering ManyToOne relationships
    (Include through models from ManyToMany relationships)
    from one 'base' object to many others.

    The URL pattern must pass a kwarg with the name `iid` that is the object from the
    `model_base` to use as the main link for the many to many relation.

    * `model_base` - mandatory - The model with the instance to be altered
    * `model_to_add` - mandatory - The model that has instances we will link to the base.
    * `template_name`
        - optional - The template used to display the form.
        - default - "aristotle_mdr/generic/actions/alter_many_to_many.html"
    * `model_base_field` - mandatory - the name of the field that goes from the `model_base` to the `model_to_add`.
    * `model_to_add_field` - mdandatory - the name of the field on the `model_to_add` model that links to the `model_base` model.
    * `ordering_field` - optional - name of the ordering field, if entered this field is hidden and updated using a drag-and-drop library
    * `form_add_another_text` - optional - string used for the button to add a new row to the form - defaults to "Add another"
    * `form_title` - Title for the form

    For example: I fwe have a mnay to many relationship from `DataElement`s to
    `Dataset`s, to alter the `DataElement`s attached to a `Dataset`, `Dataset` is the
    `base_model` and `model_to_add` is `DataElement`.
    """

    template_name = "aristotle_mdr/generic/actions/alter_one_to_many.html"
    model_to_add_field = None
    ordering_field = None
    form_add_another_text = None

    formset = None

    def get_context_data(self, **kwargs):
        context = super(GenericAlterOneToManyView, self).get_context_data(**kwargs)
        context['form_add_another_text'] = self.form_submit_text or _('Add another')

        context['formset'] = self.formset or self.get_formset()(
            queryset=getattr(self.item, self.model_base_field).all(),
            # initial=[{'order': num_values, 'value': '', 'meaning': ''}]
            )
        return context

    def get_form(self, form_class=None):
        return None

    def get_formset(self):
        kwargs = {}
        if self.ordering_field:
            kwargs['widgets'] = {self.ordering_field: forms.HiddenInput()}
        return modelformset_factory(
            self.model_to_add,
            can_delete=True,  # dont need can_order is we have an order field
            exclude=[self.model_to_add_field],
            # fields='__all__',
            extra=0,
            **kwargs
            )

    def post(self, request, *args, **kwargs):
        """
        Handles POST requests, instantiating a form instance with the passed
        POST variables and then checked for validity.
        """
        form = self.get_form()
        GenericFormSet = self.get_formset()
        self.formset = GenericFormSet(self.request.POST, self.request.FILES)
        formset = self.formset
        if formset.is_valid():
            with transaction.atomic(), reversion.revisions.create_revision():
                self.item.save()  # do this to ensure we are saving reversion records for the value domain, not just the values
                formset.save(commit=False)
                print self.item
                for form in formset.forms:
                    if form['id'].value() not in [deleted_record['id'].value() for deleted_record in formset.deleted_forms]:
                        # Don't immediately save, we need to attach the parent object
                        value = form.save(commit=False)
                        setattr(value, self.model_to_add_field, self.item)
                        value.save()
                for obj in formset.deleted_objects:
                    obj.delete()
                # formset.save(commit=True)
                reversion.revisions.set_user(request.user)
                reversion.revisions.set_comment(construct_change_message(request, None, [formset]))

            return HttpResponseRedirect(self.get_success_url())
        else:
            return self.form_invalid(form)
