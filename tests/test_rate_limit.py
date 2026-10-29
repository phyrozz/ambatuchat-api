"""Exercise the rate limiter without requiring AWS credentials."""
import concurrent.futures
from decimal import Decimal
import importlib.util
import pathlib
import sys
import threading
import types
import unittest
from unittest.mock import patch


class ClientError(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class AtomicTable:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.settings = {}

    def get_item(self, **args):
        return {'Item': {'limits': self.settings}}

    def update_item(self, **args):
        key = tuple(args['Key'].values())
        with self.lock:
            count = self.items.get(key, 0)
            if count >= args['ExpressionAttributeValues'][':limit']:
                raise ClientError('ConditionalCheckFailedException')
            self.items[key] = count + 1


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.table = AtomicTable()
        runtime = types.ModuleType('chat_runtime')
        runtime.ddb = self.table
        botocore = types.ModuleType('botocore')
        exceptions = types.ModuleType('botocore.exceptions')
        exceptions.ClientError = ClientError
        modules = {'chat_runtime': runtime, 'botocore': botocore, 'botocore.exceptions': exceptions}
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location('chat_rate_limit_test', pathlib.Path(__file__).parents[1] / 'chat_rate_limit.py')
            self.limiter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.limiter)

    def test_concurrent_requests_share_the_user_limit(self):
        def attempt(_):
            try:
                self.limiter.check_rate_limit('player', 'send')
                return True
            except ValueError as error:
                self.assertEqual(str(error), 'RATE_LIMITED')
                return False

        with patch.object(self.limiter.time, 'time', return_value=120):
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                self.assertEqual(sum(pool.map(attempt, range(100))), 30)
            self.limiter.check_rate_limit('another-player', 'send')
            self.limiter.check_rate_limit('player', 'history')

    def test_new_window_and_other_database_errors(self):
        with patch.object(self.limiter.time, 'time', return_value=120):
            for _ in range(30):
                self.limiter.check_rate_limit('player', 'send')
        with patch.object(self.limiter.time, 'time', return_value=180):
            self.limiter.check_rate_limit('player', 'send')
        with patch.object(self.table, 'update_item', side_effect=ClientError('AccessDeniedException')):
            with self.assertRaises(ClientError):
                self.limiter.check_rate_limit('player', 'send')

    def test_settings_apply_to_the_next_request(self):
        self.table.settings = {'send': {'limit': Decimal(2), 'seconds': Decimal(60)}}
        with patch.object(self.limiter.time, 'time', return_value=120):
            self.limiter.check_rate_limit('player', 'send')
            self.limiter.check_rate_limit('player', 'send')
            with self.assertRaisesRegex(ValueError, 'RATE_LIMITED'):
                self.limiter.check_rate_limit('player', 'send')
            self.table.settings['send']['limit'] = Decimal(3)
            self.limiter.check_rate_limit('player', 'send')


if __name__ == '__main__':
    unittest.main()
