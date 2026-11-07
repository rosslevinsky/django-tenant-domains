from django import forms
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from django_tenant_domains.decorators import tenant_permission_required
from django_tenant_domains.permissions import user_has_tenant_perm

from .models import Contact


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["name", "email", "phone", "notes"]


def _to_list(request):
    return redirect("contacts", tenant_slug=request.tenant.slug)


@tenant_permission_required("address_book.view_contact")
def contact_list(request, tenant_slug):
    contacts = Contact.objects.all()
    query = request.GET.get("q", "").strip()
    if query:
        contacts = contacts.filter(Q(name__icontains=query) | Q(email__icontains=query))
    context = {
        "contacts": contacts,
        "query": query,
        "can_add": user_has_tenant_perm(request.user, "address_book.add_contact"),
        "can_change": user_has_tenant_perm(request.user, "address_book.change_contact"),
        "can_delete": user_has_tenant_perm(request.user, "address_book.delete_contact"),
        "can_invite": user_has_tenant_perm(request.user, "django_tenant_domains.add_invitation"),
    }
    return render(request, "address_book/contact_list.html", context)


def _edit(request, contact, title):
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        form.save()
        return _to_list(request)
    context = {"form": form, "title": title, "contact": contact}
    return render(request, "address_book/contact_form.html", context)


@tenant_permission_required("address_book.add_contact")
def contact_add(request, tenant_slug):
    return _edit(request, None, "Add a contact")


@tenant_permission_required("address_book.change_contact")
def contact_edit(request, tenant_slug, pk):
    return _edit(request, get_object_or_404(Contact.objects, pk=pk), "Edit contact")


@tenant_permission_required("address_book.delete_contact")
def contact_delete(request, tenant_slug, pk):
    contact = get_object_or_404(Contact.objects, pk=pk)
    if request.method == "POST":
        contact.delete()
        return _to_list(request)
    return render(request, "address_book/contact_delete.html", {"contact": contact})
