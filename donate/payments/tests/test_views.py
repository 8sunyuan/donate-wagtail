from decimal import Decimal
from unittest import mock

from django.http import Http404
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.generic import FormView

from braintree import ErrorCodes, ErrorResult
from freezegun import freeze_time
from freezegun.api import FakeDate

from ..forms import (
    BraintreePaymentForm, BraintreeCardPaymentForm, BraintreePaypalPaymentForm,
    UpsellForm
)
from ..views import (
    BraintreePaymentMixin, CardPaymentView, CardUpsellView, PaypalPaymentView,
    TransactionRequiredMixin
)
from ..exceptions import InvalidAddress


class MockBraintreeTransaction:
    id = 'transaction-id-1'


class MockBraintreeResult:
    is_success = True
    transaction = MockBraintreeTransaction()


class MockBraintreeSubscription:
    id = 'subscription-id-1'


class MockBraintreePaymentMethod:
    token = 'payment-method-1'


class MockBraintreeCustomer:
    payment_methods = [
        MockBraintreePaymentMethod()
    ]


class MockBraintreeSubscriptionResult:
    is_success = True
    subscription = MockBraintreeSubscription()


class BraintreeMixinTestView(BraintreePaymentMixin, FormView):

    def get_transaction_details_for_session(self, result, form):
        return {
            'amount': '50',
            'some': 'data'
        }


class BraintreeMixinTestCase(TestCase):

    def test_success_stores_transaction_details_to_session(self):
        form = BraintreePaymentForm({'braintree_nonce': 'hello-braintree', 'amount': 10})
        assert form.is_valid()

        view = BraintreeMixinTestView()
        view.request = RequestFactory().get('/')
        view.request.session = {}
        view.success(MockBraintreeResult(), form)

        self.assertEqual(view.request.session['completed_transaction_details'], {
            'amount': '50',
            'some': 'data',
        })


class CardPaymentViewTestCase(TestCase):

    def setUp(self):
        self.form_data = {
            'first_name': 'Alice',
            'last_name': 'Bob',
            'email': 'alice@example.com',
            'phone_number': '+442088611222',
            'address_line_1': '1 Oak Tree Hill',
            'town': 'New York',
            'post_code': '10022',
            'country': 'US',
            'amount': 50,
            'braintree_nonce': 'hello-braintree',
        }

        self.request = RequestFactory().get('/')
        self.request.session = {}
        self.view = CardPaymentView()
        self.view.payment_frequency = 'single'
        self.view.currency = 'usd'
        self.view.request = self.request

        self.fake_error_result = ErrorResult("gateway", {
            'message': 'Some error',
            'errors': {
                'credit_card': {
                    'errors': [
                        {
                            'code': ErrorCodes.CreditCard.CreditCardTypeIsNotAccepted,
                            'message': 'Type not accepted',
                        },
                        {
                            'code': ErrorCodes.CreditCard.CustomerIdIsInvalid,
                            'message': 'Invalid Customer ID',
                        }
                    ]
                }
            }
        })

    def test_404_for_invalid_frequency(self):
        request = RequestFactory().get('/')
        view = CardPaymentView()
        view.request = request
        with self.assertRaises(Http404):
            view.dispatch(request, frequency='yearly')

    def test_bad_amount_redirects(self):
        request = RequestFactory().get('/?amount=foo')
        view = CardPaymentView()
        view.request = request
        response = view.dispatch(request, frequency='monthly')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    def test_filter_user_card_errors(self):
        filtered = CardPaymentView().filter_user_card_errors(self.fake_error_result)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0], 'The type of card you used is not accepted.')

    def test_filtered_errors_returned_as_form_errors(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        self.view.process_braintree_error_result(self.fake_error_result, form)
        self.assertEqual(
            form.errors,
            {'__all__': ['The type of card you used is not accepted.']}
        )

    def test_generic_error_message_if_no_reportable_errors(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        self.view.process_braintree_error_result(ErrorResult("gateway", {
            'message': 'Some system error',
            'errors': {}
        }), form)

        self.assertEqual(
            form.errors,
            {'__all__': ['Sorry there was an error processing your payment. '
                         'Please try again later or use a different payment method.']}
        )

    def test_check_for_address_errors_with_no_address_related_errors(self):
        result = ErrorResult("gateway", {
            'message': 'Some error',
            'errors': {
                'credit_card': {
                    'errors': [
                        {
                            'code': ErrorCodes.CreditCard.CreditCardTypeIsNotAccepted,
                            'message': 'Type not accepted',
                        },
                        {
                            'code': ErrorCodes.CreditCard.CustomerIdIsInvalid,
                            'message': 'Invalid Customer ID',
                        }
                    ]
                }
            }
        })
        view = CardPaymentView()
        self.assertIsNone(view.check_for_address_errors(result))

    def test_check_for_address_errors_with_address_related_errors(self):
        result = ErrorResult("gateway", {
            'message': 'Some error',
            'errors': {
                'address': {
                    'errors': [
                        {
                            'code': ErrorCodes.Address.PostalCodeInvalidCharacters,
                            'message': 'invalid post code',
                        }
                    ]
                }
            }
        })
        view = CardPaymentView()
        with self.assertRaises(InvalidAddress):
            view.check_for_address_errors(result)

    def test_gateway_address_errors_triggers_report_invalid_address(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        result = ErrorResult("gateway", {
            'message': 'Some error',
            'errors': {
                'address': {
                    'errors': [
                        {
                            'code': ErrorCodes.Address.PostalCodeInvalidCharacters,
                            'message': 'invalid post code',
                        }
                    ]
                }
            }
        })

        self.view.process_braintree_error_result(result, form)
        self.assertEqual(len(self.view.gateway_address_errors), 1)

    def test_get_custom_fields(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()
        custom_fields = self.view.get_custom_fields(form)
        self.assertEqual(custom_fields, {})

    def test_get_address_info(self):
        info = self.view.get_address_info(self.form_data)
        self.assertEqual(info, {
            'street_address': self.form_data['address_line_1'],
            'locality': self.form_data['town'],
            'postal_code': self.form_data['post_code'],
            'country_code_alpha2': self.form_data['country'],
        })

    def test_create_customer(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            self.view.create_customer(form)

        mock_gateway.customer.create.assert_called_once_with({
            'first_name': self.form_data['first_name'],
            'last_name': self.form_data['last_name'],
            'email': self.form_data['email'],
            'payment_method_nonce': 'hello-braintree',
            'custom_fields': {},
            'credit_card': {
                'billing_address': {
                    'street_address': self.form_data['address_line_1'],
                    'locality': self.form_data['town'],
                    'postal_code': self.form_data['post_code'],
                    'country_code_alpha2': self.form_data['country'],
                }
            }
        })


class SingleCardPaymentViewTestCase(CardPaymentViewTestCase):

    def setUp(self):
        super().setUp()
        self.view.payment_frequency = 'single'

    def test_transaction_data_submitted_to_braintree(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        with mock.patch.object(CardPaymentView, 'create_customer') as mock_create_customer:
            mock_create_customer.return_value.is_success = True
            mock_create_customer.return_value.customer = MockBraintreeCustomer()
            with mock.patch('donate.payments.views.gateway') as mock_gateway:
                self.view.form_valid(form)

        mock_gateway.transaction.sale.assert_called_once_with({
            'merchant_account_id': 'usd-ac',
            'payment_method_token': 'payment-method-1',
            'amount': Decimal(50),
            'options': {
                'submit_for_settlement': True,
            }
        })

    def test_get_transaction_id(self):
        self.assertEqual(
            self.view.get_transaction_id(MockBraintreeResult()),
            'transaction-id-1'
        )

    def test_get_success_url(self):
        self.assertEqual(
            self.view.get_success_url(),
            reverse('payments:card_upsell')
        )


class MonthlyCardPaymentViewTestCase(CardPaymentViewTestCase):

    def setUp(self):
        super().setUp()
        self.view.payment_frequency = 'monthly'

    def test_subscription_data_submitted_to_braintree(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        with mock.patch.object(CardPaymentView, 'create_customer') as mock_create_customer:
            mock_create_customer.return_value.is_success = True
            mock_create_customer.return_value.customer = MockBraintreeCustomer()
            with mock.patch('donate.payments.views.gateway') as mock_gateway:
                self.view.form_valid(form)

        mock_gateway.subscription.create.assert_called_once_with({
            'plan_id': 'usd-plan',
            'merchant_account_id': 'usd-ac',
            'payment_method_token': 'payment-method-1',
            'price': 50,
        })

    def test_failed_customer_creation_calls_error_processor(self):
        form = BraintreeCardPaymentForm(self.form_data)
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            mock_gateway.customer.create.return_value.is_success = False
            response = self.view.form_valid(form)

        self.assertFalse(form.is_valid())
        self.assertTrue(response.status_code, 200)

    def test_get_transaction_id(self):
        self.assertEqual(
            self.view.get_transaction_id(MockBraintreeSubscriptionResult()),
            'subscription-id-1'
        )

    def test_get_success_url(self):
        self.assertEqual(
            self.view.get_success_url(),
            reverse('payments:completed')
        )


class PaypalPaymentViewTestCase(TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/')
        self.request.session = self.client.session
        self.view = PaypalPaymentView()
        self.view.request = self.request

    def test_transaction_data_submitted_to_braintree(self):
        form = BraintreePaypalPaymentForm(
            {'braintree_nonce': 'hello-braintree', 'amount': 10, 'frequency': 'single'}
        )
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            mock_gateway.transaction.sale.return_value = MockBraintreeResult()
            self.view.form_valid(form)

        mock_gateway.transaction.sale.assert_called_once_with({
            'amount': 10,
            'custom_fields': {},
            'payment_method_nonce': 'hello-braintree',
            'options': {'submit_for_settlement': True}
        })

    def test_subscription_data_submitted_to_braintree(self):
        form = BraintreePaypalPaymentForm(
            {'braintree_nonce': 'hello-braintree', 'amount': 10, 'frequency': 'monthly'}
        )
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            mock_gateway.customer.create.return_value.is_success = True
            mock_gateway.customer.create.return_value.customer = MockBraintreeCustomer()
            self.view.form_valid(form)

        mock_gateway.customer.create.assert_called_once_with({
            'payment_method_nonce': 'hello-braintree',
            'custom_fields': {},
        })

        mock_gateway.subscription.create.assert_called_once_with({
            'plan_id': 'usd',
            'payment_method_token': 'payment-method-1',
            'price': 10,
        })

    def test_failed_customer_creation_calls_error_processor(self):
        form = BraintreePaypalPaymentForm(
            {'braintree_nonce': 'hello-braintree', 'amount': 10, 'frequency': 'monthly'}
        )
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            mock_gateway.customer.create.return_value.is_success = False
            response = self.view.form_valid(form)

        self.assertTrue(response.status_code, 200)

    def test_get_transaction_details_for_session(self):
        form = BraintreePaypalPaymentForm(
            {'braintree_nonce': 'hello-braintree', 'amount': 10, 'frequency': 'monthly'}
        )
        assert form.is_valid()
        self.view.frequency = 'monthly'
        self.assertEqual(
            self.view.get_transaction_details_for_session(MockBraintreeSubscriptionResult(), form),
            {
                'amount': 10,
                'transaction_id': 'subscription-id-1',
                'payment_method': 'paypal',
            }
        )


class TransactionRequiredMixinTestCase(TestCase):

    def test_missing_transaction_redirects(self):
        view = TransactionRequiredMixin()
        view.request = RequestFactory().get('/')
        view.request.session = {}
        response = view.dispatch(view.request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')


class CardUpsellViewTestCase(TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/')
        self.request.session = {
            'completed_transaction_details': {
                'first_name': 'Alice',
                'last_name': 'Bob',
                'email': 'alice@example.com',
                'phone_number': '+442088611222',
                'address_line_1': '1 Oak Tree Hill',
                'town': 'New York',
                'post_code': '10022',
                'country': 'US',
                'amount': 50,
                'currency': 'usd',
                'payment_frequency': 'single',
                'payment_method': 'card',
                'payment_method_token': 'payment-method-1',
            }
        }
        self.view = CardUpsellView()
        self.view.request = self.request

    def test_skips_if_previous_transaction_was_not_card(self):
        self.request.session['completed_transaction_details']['payment_method'] = 'paypal'
        response = self.view.get(self.request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('payments:completed'))

    def test_skips_if_previous_transaction_was_not_single(self):
        self.request.session['completed_transaction_details']['payment_frequency'] = 'monthly'
        response = self.view.get(self.request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('payments:completed'))

    @freeze_time('2019-07-26')
    def test_subscription_data_submitted_to_braintree(self):
        form = UpsellForm({'amount': Decimal(15)})
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            self.view.form_valid(form)

        mock_gateway.subscription.create.assert_called_once_with({
            'plan_id': 'usd-plan',
            'merchant_account_id': 'usd-ac',
            'payment_method_token': 'payment-method-1',
            'price': Decimal(15),
            'first_billing_date': FakeDate(2019, 8, 26)
        })

    def test_failed_customer_creation_calls_error_processor(self):
        form = UpsellForm({'amount': Decimal(15)})
        assert form.is_valid()

        with mock.patch('donate.payments.views.gateway') as mock_gateway:
            mock_gateway.subscription.create.return_value.is_success = False
            response = self.view.form_valid(form)

        self.assertFalse(form.is_valid())
        self.assertTrue(response.status_code, 200)

    def test_get_transaction_details_for_session(self):
        form = UpsellForm({'amount': Decimal(17)})
        assert form.is_valid()

        mock_result = MockBraintreeSubscriptionResult()
        self.assertEqual(
            self.view.get_transaction_details_for_session(mock_result, form, currency='usd'),
            {
                'amount': Decimal(17),
                'transaction_id': 'subscription-id-1',
                'payment_method': 'card',
                'currency': 'usd',
                'payment_frequency': 'monthly',
            }
        )
