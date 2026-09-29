import io,unittest
from unittest.mock import patch
import fare_aggregator as fa
class Response(io.BytesIO):
 status=200
class Telegram(unittest.TestCase):
 def test_http_200_without_api_ok_is_not_delivery(self):
  with patch.object(fa.urllib.request,'urlopen',return_value=Response(b'{"ok":false,"description":"failure"}')):
   self.assertFalse(fa.send_telegram('fixture','123','text'))
 def test_message_id_logged_without_token(self):
  with patch.object(fa.urllib.request,'urlopen',return_value=Response(b'{"ok":true,"result":{"message_id":456}}')),patch.object(fa,'log') as log:
   self.assertTrue(fa.send_telegram('fixture-secret','123','text'))
   self.assertIn('456',str(log.call_args_list));self.assertNotIn('fixture-secret',str(log.call_args_list))
