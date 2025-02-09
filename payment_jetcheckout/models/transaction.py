# -*- coding: utf-8 -*-
from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError
from datetime import date
import requests
import json
import re

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _calc_is_jetcheckout(self):
        for rec in self:
            rec.is_jetcheckout = rec.acquirer_id.provider == 'jetcheckout'

    @api.model
    def _get_default_partner_country_id(self):
        country = self.env.company.country_id
        if not country:
            raise ValidationError(_('Please define a country for this company'))
        return country.id

    is_jetcheckout = fields.Boolean(compute='_calc_is_jetcheckout')
    jetcheckout_card_name = fields.Char('Card Holder Name', readonly=True)
    jetcheckout_card_number = fields.Char('Card Number', readonly=True)
    jetcheckout_card_type = fields.Char('Card Type', readonly=True)
    jetcheckout_vpos_name = fields.Char('Virtual Pos', readonly=True)
    jetcheckout_order_id = fields.Char('Order', readonly=True)
    jetcheckout_ip_address = fields.Char('IP Address', readonly=True)
    jetcheckout_transaction_id = fields.Char('Transaction', readonly=True)
    jetcheckout_payment_amount = fields.Monetary('Payment Amount', readonly=True)
    jetcheckout_installment_count = fields.Integer('Installment Count', readonly=True)
    jetcheckout_installment_amount = fields.Monetary('Installment Amount', readonly=True)
    jetcheckout_commission_rate = fields.Float('Commission Rate', readonly=True)
    jetcheckout_commission_amount = fields.Monetary('Commission Amount', readonly=True)

    def _jetcheckout_s2s_get_tx_status(self):
        url = '%s/api/v1/payment/status' % self.acquirer_id._get_jetcheckout_api_url()
        data = {
            "application_key": self.acquirer_id.jetcheckout_api_key,
            "order_id": self.jetcheckout_order_id,
            "lang": "tr",
        }

        response = requests.post(url, data=json.dumps(data))
        if response.status_code == 200:
            result = response.json()
            if int(result['response_code']) == 200:
                values = {'result': result}
            else:
                values = {'error': _('%s (Error Code: %s)') % (result['message'], result['response_code'])}
        else:
            values = {'error': _('%s (Error Code: %s)') % (response.reason, response.status_code)}
        return values

    def _jetcheckout_create_payment(self):
        line = self.env['payment.acquirer.jetcheckout.journal'].sudo().search([
            ('acquirer_id','=',self.acquirer_id.id),
            ('pos_id.name','=',self.jetcheckout_vpos_name),
        ], limit=1)
        if not line:
            return False

        payment = self.env['account.payment'].sudo().create({
            'partner_id': self.partner_id.id,
            'partner_type': 'customer',
            'amount': self.amount + self.fees,
            'currency_id': self.currency_id.id,
            'date': date.today(),
            'payment_type': 'inbound',
            'journal_id': line.journal_id.id,
            'payment_method_id': self.env.ref('payment_jetcheckout.payment_method_jetcheckout').id,
            'ref': self.reference,
            #'invoice_ids': [(6, 0, self.invoice_ids.ids)],
        })
        try:
            payment.post_with_jetcheckout(line, self.fees, self.jetcheckout_commission_amount, self.jetcheckout_ip_address)
        except Exception as e:
            raise ValidationError(str(e))
        return payment

    def jetcheckout_s2s_do_refund(self, amount=0.0, **kwargs):
        self.ensure_one()
        url = '%s/api/v1/payment/refund' % self.acquirer_id._get_jetcheckout_api_url()
        data = {
            "application_key": self.acquirer_id.jetcheckout_api_key,
            "order_id": self.jetcheckout_order_id,
            "transaction_id": self.jetcheckout_transaction_id,
            "amount": int(amount * 100),
            "currency": self.currency_id.name,
            "language": "tr",
        }

        response = requests.post(url, data=json.dumps(data))
        if response.status_code == 200:
            result = response.json()
            if int(result['response_code']) == 0:
                values = {'result': result}
            else:
                values = {'error': _('%s (Error Code: %s)') % (result['message'], result['response_code'])}
        else:
            values = {'error': _('%s (Error Code: %s)') % (response.reason, response.status_code)}

        if 'error' in values:
            raise UserError(values['error'])
        self.write({
            'state': 'cancel',
            'state_message': _('Transaction has been refunded successfully.')
        })

    def jetcheckout_s2s_do_cancel(self, **kwargs):
        self.ensure_one()
        url = '%s/api/v1/payment/cancel' % self.acquirer_id._get_jetcheckout_api_url()
        data = {
            "application_key": self.acquirer_id.jetcheckout_api_key,
            "order_id": self.jetcheckout_order_id,
            "transaction_id": self.jetcheckout_transaction_id,
            "language": "tr",
        }

        response = requests.post(url, data=json.dumps(data))
        if response.status_code == 200:
            result = response.json()
            if int(result['response_code']) == 0:
                values = {'result': result}
            else:
                values = {'error': _('%s (Error Code: %s)') % (result['message'], result['response_code'])}
        else:
            values = {'error': _('%s (Error Code: %s)') % (response.reason, response.status_code)}
        return values

    def jetcheckout_validate_order(self):
        self.ensure_one()
        orders = self.sale_order_ids
        if not orders:
            return
        orders.with_context(send_email=True).action_confirm()

    def jetcheckout_payment(self):
        self.ensure_one()

        try:
            payment = self.sudo()._jetcheckout_create_payment()
        except Exception as e:
            self.write({
                'state_message': _('Transaction is succesful, but payment could not be validated. Probably one of partner or journal accounts are missing') + '\n' + re.sub('( None)*[^a-z A-Z]+','', str(e)),
            })
            return

        if payment:
            self.write({
                'payment_id': payment.id,
                'state_message': _('Transaction is succesful and payment has been validated.'),
            })
        else:
            self.write({
                'state_message': _('Transaction is succesful, but payment could not be validated. Probably one of partner or journal accounts are missing')
            })

    def jetcheckout_cancel(self):
        self.ensure_one()
        values = self.jetcheckout_s2s_do_cancel()
        if 'error' in values:
            raise UserError(values['error'])
        self.write({
            'state': 'cancel',
            'state_message': _('Transaction has been cancelled successfully.')
        })

    def jetcheckout_refund(self):
        self.ensure_one()
        vals = {
            'transaction_id': self.id,
            'total': self.amount,
            'currency_id': self.currency_id.id,
        }
        refund = self.env['payment.acquirer.jetcheckout.refund'].create(vals)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'payment.acquirer.jetcheckout.refund',
            'res_id': refund.id,
            'name': _('Create refund for %s') % self.reference,
            'view_mode': 'form',
            'target': 'new',
        }

    def jetcheckout_query(self):
        self.ensure_one()
        values = self._jetcheckout_s2s_get_tx_status()
        if 'error' in values:
            raise UserError(values['error'])

        result = values['result']
        if result['successful']:
            if result['cancelled']:
                state = 'cancel'
            else:
                state = 'done'
        else:
            state = 'error'
        self.write({'state': state})

        vals = {
            'transaction_date': result['transaction_date'][:19],
            'vpos_id': result['virtual_pos_name'],
            'is_successful': result['successful'],
            'is_completed': result['completed'],
            'is_cancelled': result['cancelled'],
            'is_3d': result['is_3d'],
            'amount': result['amount'],
            'commission': result['commission_amount'],
            'cost_rate': result['expected_cost_rate'],
            'auth_code': result['auth_code'],
            'service_ref_id': result['service_ref_id'],
            'currency_id': self.currency_id.id,
        }
        status = self.env['payment.acquirer.jetcheckout.status'].create(vals)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'payment.acquirer.jetcheckout.status',
            'res_id': status.id,
            'name': _('%s Transaction Status') % self.reference,
            'view_mode': 'form',
            'target': 'new',
        }
