import types
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from . import billing
from .models import Client, Invoice, Item, User, WorkNote


class _SO(dict):
    """Stand-in for a Stripe object: dict-like AND attribute access, like the real one."""
    __getattr__ = dict.get


def _fake_stripe(existing_by_email=None, sub_customer=None):
    """A stripe module stub. Records calls and returns SO objects. Optionally seeds
    existing customers (by email) and a subscription's customer for dedup tests."""
    fake = types.SimpleNamespace(
        api_key=None, items=[], invoice_kwargs=None, modified=[], created=[],
    )
    existing_by_email = existing_by_email or {}

    def _cust_create(**kw):
        fake.created.append(kw)
        return _SO(id="cus_TEST")

    def _cust_modify(cid, **kw):
        fake.modified.append((cid, kw))
        return _SO(id=cid, **kw)

    def _cust_list(email=None, limit=None):
        cid = existing_by_email.get(email)
        return _SO(data=[_SO(id=cid)] if cid else [])

    def _sub_retrieve(sid):
        return _SO(customer=sub_customer)

    def _invoice(**kw):
        fake.invoice_kwargs = kw
        return _SO(id="in_TEST", hosted_invoice_url="")

    def _item(**kw):
        fake.items.append(kw)
        return _SO(id=f"ii_{len(fake.items)}")

    fake.Customer = types.SimpleNamespace(create=_cust_create, modify=_cust_modify, list=_cust_list)
    fake.Subscription = types.SimpleNamespace(retrieve=_sub_retrieve)
    fake.Invoice = types.SimpleNamespace(create=_invoice)
    fake.InvoiceItem = types.SimpleNamespace(create=_item)
    return fake


@override_settings(STRIPE_SECRET_KEY="sk_test_fake", INVOICE_DAYS_UNTIL_DUE=14)
class BillingFlowTests(TestCase):
    def setUp(self):
        self.mark = User.objects.create_user("mark", password="x", role=User.Role.OPERATOR)
        self.hourly = Client.objects.create(
            name="Drop-in Radio", billing_mode=Client.BillingMode.HOURLY,
            hourly_rate=Decimal("125.00"),
        )
        self.retainer = Client.objects.create(
            name="Ascend", billing_mode=Client.BillingMode.CONTRACT,
        )
        item = Item.objects.create(title="Fix stream", client=self.hourly, priority="M")
        for h in ("4.00", "3.25", "10.00"):
            WorkNote.objects.create(item=item, author=self.mark, body="work", hours=Decimal(h))
        self.start, self.end = date(2026, 7, 1), date(2026, 7, 31)

    def test_creates_draft_invoice_and_marks_hours(self):
        fake = _fake_stripe()
        with mock.patch.object(billing, "stripe", fake):
            inv = billing.create_draft_invoice(self.hourly, self.start, self.end, user=self.mark)

        # 17.25h × $125 = $2156.25
        self.assertEqual(inv.total_hours, Decimal("17.25"))
        self.assertEqual(inv.amount, Decimal("2156.25"))
        self.assertEqual(inv.status, Invoice.Status.DRAFT)
        self.assertEqual(inv.stripe_invoice_id, "in_TEST")
        # created as a DRAFT the operator reviews, sent via invoice email
        self.assertIs(fake.invoice_kwargs["auto_advance"], False)
        self.assertEqual(fake.invoice_kwargs["collection_method"], "send_invoice")
        # line items are integer cents and sum to the total
        self.assertTrue(all(isinstance(i["amount"], int) for i in fake.items))
        self.assertEqual(sum(i["amount"] for i in fake.items), 215625)
        # customer id cached on the client
        self.hourly.refresh_from_db()
        self.assertEqual(self.hourly.stripe_customer_id, "cus_TEST")
        # all three worknotes stamped invoiced
        self.assertEqual(WorkNote.objects.filter(invoice=inv).count(), 3)

    def test_no_double_billing(self):
        fake = _fake_stripe()
        with mock.patch.object(billing, "stripe", fake):
            billing.create_draft_invoice(self.hourly, self.start, self.end, user=self.mark)
            with self.assertRaisesMessage(billing.BillingError, "No unbilled hours"):
                billing.create_draft_invoice(self.hourly, self.start, self.end, user=self.mark)

    def test_retainer_client_rejected(self):
        fake = _fake_stripe()
        with mock.patch.object(billing, "stripe", fake):
            with self.assertRaises(billing.BillingError):
                billing.create_draft_invoice(self.retainer, self.start, self.end, user=self.mark)

    def test_missing_key_is_friendly_error(self):
        with override_settings(STRIPE_SECRET_KEY=""):
            with self.assertRaisesMessage(billing.BillingError, "No Stripe key configured"):
                billing.create_draft_invoice(self.hourly, self.start, self.end, user=self.mark)

    def test_subscription_client_listed_but_exempt(self):
        # Ascend is on a Stripe subscription: shown for reference, never hourly-billed.
        self.retainer.stripe_subscription_id = "sub_ABC"
        self.retainer.save()
        item = Item.objects.create(title="Monthly check", client=self.retainer, priority="L")
        WorkNote.objects.create(item=item, author=self.mark, body="patching", hours=Decimal("2.00"))

        self.client.force_login(self.mark)
        resp = self.client.get("/billing/", {"year": 2026, "month": 7})
        self.assertContains(resp, "On subscription")
        self.assertContains(resp, "Ascend")           # listed for reference
        self.assertContains(resp, "sub_ABC")          # subscription id surfaced + linked

        # …and it's still exempt from an actual billing run.
        with mock.patch.object(billing, "stripe", _fake_stripe()):
            with self.assertRaises(billing.BillingError):
                billing.create_draft_invoice(self.retainer, self.start, self.end, user=self.mark)


@override_settings(STRIPE_SECRET_KEY="sk_test_fake")
class ClientStripeSyncTests(TestCase):
    """TimeTrax is master → Stripe. Sync bonds clients dedup-safely."""

    def setUp(self):
        self.c = Client.objects.create(name="New Co", billing_mode=Client.BillingMode.HOURLY,
                                       hourly_rate=Decimal("100"))

    def test_creates_customer_when_none_exists(self):
        fake = _fake_stripe()
        with mock.patch.object(billing, "stripe", fake):
            action = billing.sync_client_to_stripe(self.c)
        self.assertEqual(action, "created")
        self.c.refresh_from_db()
        self.assertEqual(self.c.stripe_customer_id, "cus_TEST")
        self.assertEqual(len(fake.created), 1)

    def test_updates_when_already_linked(self):
        self.c.stripe_customer_id = "cus_EXISTING"
        self.c.save()
        fake = _fake_stripe()
        with mock.patch.object(billing, "stripe", fake):
            action = billing.sync_client_to_stripe(self.c)
        self.assertEqual(action, "updated")
        self.assertEqual(fake.modified[0][0], "cus_EXISTING")  # pushed to the same customer
        self.assertEqual(fake.created, [])                     # no duplicate created

    def test_adopts_subscription_customer_no_duplicate(self):
        # Ascend case: has a subscription already → reuse ITS customer, don't create one.
        self.c.stripe_subscription_id = "sub_ABC"
        self.c.save()
        fake = _fake_stripe(sub_customer="cus_FROM_SUB")
        with mock.patch.object(billing, "stripe", fake):
            action = billing.sync_client_to_stripe(self.c)
        self.assertEqual(action, "linked via subscription")
        self.c.refresh_from_db()
        self.assertEqual(self.c.stripe_customer_id, "cus_FROM_SUB")
        self.assertEqual(fake.created, [])

    def test_dedups_by_email(self):
        self.c.contact_email = "hi@newco.com"
        self.c.save()
        fake = _fake_stripe(existing_by_email={"hi@newco.com": "cus_BYEMAIL"})
        with mock.patch.object(billing, "stripe", fake):
            action = billing.sync_client_to_stripe(self.c)
        self.assertEqual(action, "linked by email")
        self.c.refresh_from_db()
        self.assertEqual(self.c.stripe_customer_id, "cus_BYEMAIL")
        self.assertEqual(fake.created, [])

    def test_sync_all_reports_counts(self):
        Client.objects.create(name="Another", billing_mode=Client.BillingMode.CONTRACT)
        fake = _fake_stripe()
        with mock.patch.object(billing, "stripe", fake):
            counts, errors = billing.sync_all_clients()
        self.assertEqual(errors, [])
        self.assertEqual(counts.get("created"), 2)
