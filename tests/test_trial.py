"""Reviewer credentials must never be sent to Meta's direct endpoint."""
import os
import unittest
from unittest.mock import patch
from avo import llm

class TrialRoutingTests(unittest.TestCase):
    @patch('avo.llm.load_dotenv')
    @patch('avo.llm.OpenAI')
    def test_direct_key_preserves_original_route(self, client, _):
        with patch.dict(os.environ, {'MODEL_API_KEY':'own-key'}, clear=True):
            llm.make_client()
        self.assertEqual(client.call_args.kwargs['base_url'], llm.BASE_URL)
        self.assertEqual(client.call_args.kwargs['api_key'], 'own-key')

    @patch('avo.llm.load_dotenv')
    @patch('avo.llm.OpenAI')
    def test_invitation_uses_gateway_even_if_own_key_exists(self, client, _):
        with patch.dict(os.environ, {'AVO_TRIAL_TOKEN':'invitation','MODEL_API_KEY':'own-key'}, clear=True):
            llm.make_client()
        self.assertEqual(client.call_args.kwargs['base_url'], llm.TRIAL_BASE_URL)
        self.assertEqual(client.call_args.kwargs['api_key'], 'invitation')
