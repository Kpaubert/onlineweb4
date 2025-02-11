# -*- coding: utf-8 -*-

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.signing import Signer
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import mixins, viewsets
from rest_framework.permissions import AllowAny
from watson import search as watson

from apps.events.filters import EventFilter
from apps.events.forms import CaptchaForm
from apps.events.models import AttendanceEvent, Attendee, Event
from apps.events.pdf_generator import EventPDF
from apps.events.serializers import EventSerializer
from apps.events.utils import (
    handle_attend_event_payment,
    handle_attendance_event_detail,
    handle_event_ajax,
    handle_event_payment,
    handle_mail_participants,
)
from apps.payment.models import Payment, PaymentDelay, PaymentRelation

from .utils import EventCalendar


def index(request):
    context = {}
    if request.user and request.user.is_authenticated:
        signer = Signer()
        context["signer_value"] = signer.sign(request.user.username)
        context["personal_ics_path"] = request.build_absolute_uri(
            reverse("events_personal_ics", args=(context["signer_value"],))
        )
    return render(request, "events/index.html", context)


def details(request, event_id, event_slug):
    event = get_object_or_404(Event, pk=event_id)

    # Restricts access to the event if it is group restricted
    if not event.can_display(request.user):
        messages.error(request, "Du har ikke tilgang til dette arrangementet.")
        return index(request)

    if request.method == "POST":
        if request.is_ajax and "action" in request.POST and "extras_id" in request.POST:
            return JsonResponse(
                handle_event_ajax(
                    event,
                    request.user,
                    request.POST["action"],
                    request.POST["extras_id"],
                )
            )

    form = CaptchaForm(user=request.user)
    context = {
        "captcha_form": form,
        "event": event,
        "ics_path": request.build_absolute_uri(reverse("event_ics", args=(event.id,))),
    }

    if event.is_attendance_event():
        try:
            payment = Payment.objects.get(
                content_type=ContentType.objects.get_for_model(AttendanceEvent),
                object_id=event_id,
            )
        except Payment.DoesNotExist:
            payment = None

        context = handle_attendance_event_detail(event, request.user, context)
        if payment:
            request.session["payment_id"] = payment.id
            context = handle_event_payment(event, request.user, payment, context)

    return render(request, "events/details.html", context)


def get_attendee(attendee_id):
    return get_object_or_404(Attendee, pk=attendee_id)


@login_required
def attend_event(request, event_id):
    event = get_object_or_404(Event, pk=event_id)

    if not event.is_attendance_event():
        messages.error(request, _("Dette er ikke et påmeldingsarrangement."))
        return redirect(event)

    if not request.POST:
        messages.error(request, _("Vennligst fyll ut skjemaet."))
        return redirect(event)

    form = CaptchaForm(request.POST, user=request.user)

    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)

        return redirect(event)

    # Check if the user is eligible to attend this event.
    # If not, an error message will be present in the returned dict
    attendance_event = event.attendance_event

    response = event.attendance_event.is_eligible_for_signup(request.user)

    if response.status:
        attendee = Attendee(event=attendance_event, user=request.user)
        if "note" in form.cleaned_data:
            attendee.note = form.cleaned_data["note"]
        attendee.show_as_attending_event = (
            request.user.get_visible_as_attending_events()
        )
        attendee.save()
        messages.success(request, _("Du er nå meldt på arrangementet."))

        if attendance_event.payment():
            handle_attend_event_payment(event, request.user)

        return redirect(event)
    else:
        messages.error(request, response.message)
        return redirect(event)


@login_required
def unattend_event(request, event_id):
    event = get_object_or_404(Event, pk=event_id)

    if not event.is_attendance_event():
        messages.error(request, _("Dette er ikke et påmeldingsarrangement."))
        return redirect(event)

    attendance_event = event.attendance_event

    # Check if user is attending
    if len(Attendee.objects.filter(event=attendance_event, user=request.user)) == 0:
        messages.error(request, _("Du er ikke påmeldt dette arrangementet."))
        return redirect(event)

    # Check if the deadline for unattending has passed
    if (
        attendance_event.unattend_deadline < timezone.now()
        and not attendance_event.is_on_waitlist(request.user)
    ):
        messages.error(
            request, _("Avmeldingsfristen for dette arrangementet har utløpt.")
        )
        return redirect(event)

    if attendance_event.event.event_start < timezone.now():
        messages.error(request, _("Dette arrangementet har allerede startet."))
        return redirect(event)

    try:
        payment = Payment.objects.get(
            content_type=ContentType.objects.get_for_model(AttendanceEvent),
            object_id=event_id,
        )
    except Payment.DoesNotExist:
        payment = None

    # Delete payment delays connected to the user and event
    if payment:

        payments = PaymentRelation.objects.filter(
            payment=payment, user=request.user, refunded=False
        )

        # Return if someone is trying to unatend without refunding
        if payments:
            messages.error(
                request,
                _(
                    "Du har betalt for arrangementet og må refundere før du kan melde deg av"
                ),
            )
            return redirect(event)

        delays = PaymentDelay.objects.filter(payment=payment, user=request.user)
        for delay in delays:
            delay.delete()

    Attendee.objects.get(event=attendance_event, user=request.user).delete()

    messages.success(request, _("Du ble meldt av arrangementet."))
    return redirect(event)


def search_events(request):
    query = request.GET.get("query")
    filters = {
        "future": request.GET.get("future"),
        "myevents": request.GET.get("myevents"),
    }
    events = _search_indexed(request, query, filters)

    return render(request, "events/search.html", {"events": events})


def _search_indexed(request, query, filters):
    results = []
    kwargs = {}
    order_by = "event_start"

    if filters["future"] == "true":
        kwargs["event_start__gte"] = timezone.now()
    else:
        # Reverse order when showing all events
        order_by = "-" + order_by

    if filters["myevents"] == "true":
        kwargs["attendance_event__attendees__user"] = request.user

    events = (
        Event.objects.filter(**kwargs)
        .order_by(order_by)
        .prefetch_related(
            "attendance_event",
            "attendance_event__attendees",
            "attendance_event__reserved_seats",
            "attendance_event__reserved_seats__reservees",
        )
    )

    # Filters events that are restricted
    display_events = set()

    for event in events:
        if event.can_display(request.user):
            display_events.add(event.pk)

    events = events.filter(pk__in=display_events)

    if query:
        for result in watson.search(query, models=(events,)):
            results.append(result.object)
        return results[:10]

    return events


@login_required()
def generate_pdf(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    # If this is not an attendance event, redirect to event with error
    if not event.attendance_event:
        messages.error(request, _("Dette er ikke et påmeldingsarrangement."))
        return redirect(event)

    if request.user.has_perm("events.change_event", obj=event):
        return EventPDF(event).render_pdf()

    messages.error(
        request, _("Du har ikke tilgang til listen for dette arrangementet.")
    )
    return redirect(event)


@login_required()
def generate_json(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    # If this is not an attendance event, redirect to event with error
    if not event.attendance_event:
        messages.error(request, _("Dette er ikke et påmeldingsarrangement."))
        return redirect(event)

    # Check access
    if not request.user.has_perm("events.change_event", obj=event):
        messages.error(
            request, _("Du har ikke tilgang til listen for dette arrangementet.")
        )
        return redirect(event)

    attendee_unsorted = event.attendance_event.attending_attendees_qs
    attendee_sorted = sorted(
        attendee_unsorted, key=lambda attendee: attendee.user.last_name
    )
    waiters = event.attendance_event.waitlist_qs
    reserve = event.attendance_event.reservees_qs
    # Goes though attendance, the waitlist and reservations, and adds them to a json file.
    attendees = []
    for a in attendee_sorted:
        attendees.append(
            {
                "first_name": a.user.first_name,
                "last_name": a.user.last_name,
                "year": a.user.year,
                "email": a.user.primary_email,
                "phone_number": a.user.phone_number,
                "allergies": a.user.allergies,
            }
        )
    waitlist = []
    for w in waiters:
        waitlist.append(
            {
                "first_name": w.user.first_name,
                "last_name": w.user.last_name,
                "year": w.user.year,
                "phone_number": w.user.phone_number,
            }
        )

    reservees = []
    for r in reserve:
        reservees.append({"name": r.name, "note": r.note})

    response = HttpResponse(content_type="application/json")
    response["Content-Disposition"] = (
        'attachment; filename="' + str(event.id) + '.json"'
    )
    response.write(
        json.dumps(
            {"Attendees": attendees, "Waitlist": waitlist, "Reservations": reservees}
        )
    )

    return response


def calendar_export(request, event_id=None, user=None):
    calendar = EventCalendar()
    if event_id:
        # Single event
        calendar.event(event_id)
    elif user:
        # Personalized calendar
        calendar.user(user)
    else:
        # All events that haven't ended yet
        calendar.events()
    return calendar.response()


@login_required
def mail_participants(request, event_id):
    event = get_object_or_404(Event, pk=event_id)

    # If this is not an attendance event, redirect to event with error
    if not event.is_attendance_event():
        messages.error(request, _("Dette er ikke et påmeldingsarrangement."))
        return redirect(event)

    # Check access
    if not request.user.has_perm("events.change_event", obj=event):
        messages.error(request, _("Du har ikke tilgang til å vise denne siden."))
        return redirect(event)

    all_attendees = list(event.attendance_event.attending_attendees_qs)
    attendees_on_waitlist = list(event.attendance_event.waitlist_qs)
    attendees_not_paid = list(event.attendance_event.attendees_not_paid)

    if request.method == "POST":
        subject = request.POST.get("subject")
        message = request.POST.get("message")
        images = [
            (image.name, image.read(), image.content_type)
            for image in request.FILES.getlist("image")
        ]
        mail_sent = handle_mail_participants(
            event,
            request.POST.get("to_email"),
            subject,
            message,
            images,
            all_attendees,
            attendees_on_waitlist,
            attendees_not_paid,
        )

        if mail_sent:
            messages.success(request, _("Mailen ble sendt"))
        else:
            messages.error(
                request, _("Vi klarte ikke å sende mailene dine. Prøv igjen")
            )

    return render(
        request,
        "events/mail_participants.html",
        {
            "all_attendees": all_attendees,
            "attendees_on_waitlist": attendees_on_waitlist,
            "attendees_not_paid": attendees_not_paid,
            "event": event,
        },
    )


@login_required
def toggle_show_as_attending(request, event_id):
    event = get_object_or_404(Event, pk=event_id)

    if not event.is_attendance_event():
        messages.error(request, _("Dette er ikke et påmeldingsarrangement."))
        return redirect(event)

    attendance_event = event.attendance_event
    attendee = Attendee.objects.get(event=attendance_event, user=request.user)

    if attendee.show_as_attending_event:
        attendee.show_as_attending_event = False
        messages.success(
            request, _("Du er ikke lenger synlig som påmeldt dette arrangementet.")
        )
    else:
        attendee.show_as_attending_event = True
        messages.success(request, _("Du er nå synlig som påmeldt dette arrangementet."))

    attendee.save()
    return redirect(event)


class EventViewSet(
    viewsets.GenericViewSet, mixins.RetrieveModelMixin, mixins.ListModelMixin
):
    serializer_class = EventSerializer
    permission_classes = (AllowAny,)
    filterset_class = EventFilter
    filterset_fields = ("event_start", "event_end", "id")
    ordering_fields = (
        "event_start",
        "event_end",
        "id",
        "is_today",
        "registration_filtered",
    )
    ordering = ("-is_today", "registration_filtered", "id")

    def get_queryset(self):
        user = self.request.user
        return Event.by_registration.get_queryset_for_user(user)
