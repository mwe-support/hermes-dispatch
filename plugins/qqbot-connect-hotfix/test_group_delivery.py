"""Exercise the real QQ sender with fake HTTP and temporary owner-local state."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('qq_group_delivery_test', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from qq_group_delivery_test import group_delivery, group_results, outbound, reply_budget
from gateway.config import PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter


class Adapter(QQAdapter):
    async def _api_request(self, method, path, body=None, *args, **kwargs):
        if method == 'GET' and path.endswith('/bot_state'):
            self.state_reads += 1
            if self.state_error:
                raise RuntimeError(self.state_error)
            return {'allow_proactive_msg': self.allowed}
        self.wire.append((path, dict(body)))
        error = self.passive_error if body.get('msg_id') else self.proactive_error
        if error:
            raise RuntimeError(error)
        return {} if self.missing_ack else {'id': 'ack-'+str(len(self.wire))}

    async def _upload_local_file(self, *args, **kwargs):
        return 'answer.xlsx', {'file_info': 'fixture-file'}

    async def _upload_media(self, *args, **kwargs):
        return {'file_info': 'fixture-image'}


outbound.patch_plain_text_retry(Adapter)
outbound.patch_expired_reply_fallback(Adapter)
reply_budget.patch_reply_budget(Adapter)
group_delivery.patch_group_delivery(Adapter)
group_results.patch_group_results(Adapter)


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = Adapter(PlatformConfig(extra={'app_id':'fixture-bot','client_secret':'fixture-secret'}))
        self.a._running = True
        self.a._ws = SimpleNamespace(closed=False)
        self.a._markdown_support = False
        self.a._chat_type_map = {'group':'group','other':'group','user':'c2c'}
        self.a._last_msg_id = {'group':'msg'}
        self.a._qq_group_reply_seen = {'group':('msg',time.time())}
        self.a._qq_reply_ledger = reply_budget.Ledger(self.root/'ledger.sqlite3')
        self.a._qq_group_results_store = group_results.Store(self.root/'results')
        self.a.allowed = True
        self.a.state_reads = 0
        self.a.state_error = None
        self.a.proactive_error = None
        self.a.passive_error = None
        self.a.missing_ack = False
        self.a.wire = []
        self.remember('msg')
        self.pacer = patch.object(reply_budget.Ledger, 'group_send_delay', return_value=0)
        self.pacer.start()
        self.addCleanup(self.pacer.stop)

    def remember(self, msg, age=0):
        self.a._qq_reply_ledger.remember('fixture-bot',msg,'group','group','user',time.time()-age)

    def used(self, msg='msg'):
        with self.a._qq_reply_ledger.connect() as db:
            return db.execute('SELECT count(*) FROM requests WHERE bot=? AND msg=?',('fixture-bot',msg)).fetchone()[0]

    async def send(self, text, msg='msg', **kwargs):
        return await self.a.send('group',text,reply_to=msg,metadata={'notify':True},**kwargs)

    async def test_more_than_five_messages_and_expired_anchor_use_proactive(self):
        self.remember('old',age=7200)
        for n in range(7):
            self.assertTrue((await self.send('part '+str(n),'old')).success)
        self.assertEqual(len(self.a.wire),7)
        self.assertTrue(all(not b.get('msg_id') and not b.get('event_id') and not b.get('message_reference') for _,b in self.a.wire))
        self.assertEqual(self.used('old'),0)
        self.assertEqual(self.a.state_reads,1)

    async def test_documents_images_and_working_use_same_proactive_path(self):
        f=self.root/'answer.xlsx';f.write_bytes(b'fixture')
        self.assertTrue((await self.a.send_document('group',str(f),reply_to='msg',metadata={'notify':True})).success)
        self.assertTrue((await self.a.send_image('group','https://example.invalid/image.png',reply_to='msg')).success)
        token=group_results._TURN.set({'chat':'group','origin':'msg','command':False})
        try:
            self.assertTrue((await self.a.send('group','Working',metadata={'_interim_send':True})).success)
        finally:
            group_results._TURN.reset(token)
        self.assertEqual([b['msg_type'] for _,b in self.a.wire],[7,7,0])
        self.assertTrue(all(not b.get('msg_id') for _,b in self.a.wire))

    async def test_disabled_permission_falls_back_for_ten_minutes_not_sixty_one(self):
        self.a.allowed=False
        self.remember('ten',age=600)
        self.assertTrue((await self.send('ten minute answer','ten')).success)
        self.a._last_msg_id.clear();self.a._qq_group_reply_seen.clear()
        self.remember('old',age=3660)
        self.assertFalse((await self.send('old answer','old')).success)
        self.assertEqual([b.get('msg_id') for _,b in self.a.wire],['ten'])
        self.assertEqual(self.used('ten'),1)
        self.assertEqual(self.used('old'),0)

    async def test_unknown_state_tries_proactive_and_permission_rejection_falls_back_once(self):
        self.a.state_error='QQ Bot API error [400]: 应用无接口访问权限'
        self.a.proactive_error='QQ Bot API error [400]: 主动消息失败，无权限'
        self.assertTrue((await self.send('answer')).success)
        self.assertEqual([b.get('msg_id') for _,b in self.a.wire],[None,'msg'])
        self.assertEqual(self.used(),1)

    async def test_revoked_permission_and_passive_exhaustion_do_not_loop(self):
        self.a.proactive_error='QQ Bot API error [400]: 主动消息失败，无权限'
        self.a.passive_error='QQ Bot API error [400]: 回复消息失败，被动回复时间或者次数超过限制'
        self.assertFalse((await self.send('answer')).success)
        self.assertEqual([b.get('msg_id') for _,b in self.a.wire],[None,'msg'])
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'pending':1})

    async def test_timeout_is_uncertain_and_never_retried_as_passive(self):
        self.a.proactive_error='QQ Bot API timeout'
        self.assertFalse((await self.send('answer')).success)
        self.assertFalse((await self.send('answer')).success)
        self.assertEqual(len(self.a.wire),1)
        self.assertIsNone(self.a.wire[0][1].get('msg_id'))
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'uncertain':1})

    async def test_progress_timeout_does_not_trigger_native_chunk_retries(self):
        self.a.proactive_error='QQ Bot API timeout'
        r=await self.a.send('group','Working',reply_to='msg',metadata={'_interim_send':True})
        self.assertFalse(r.success)
        self.assertFalse(r.retryable)
        self.assertEqual(len(self.a.wire),1)

    async def test_proactive_rate_rejection_does_not_switch_to_passive(self):
        self.a.proactive_error='QQ Bot API error [400]: 主动消息发送超过频控限制'
        self.assertFalse((await self.send('answer')).success)
        self.assertEqual(len(self.a.wire),1)
        self.assertIsNone(self.a.wire[0][1].get('msg_id'))
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'pending':1})

    async def test_permission_enablement_is_observed_after_cache_expiry(self):
        self.a.allowed=False
        self.assertTrue((await self.send('before')).success)
        self.a.allowed=True
        future=time.monotonic()+61
        with patch.object(group_delivery,'time',SimpleNamespace(monotonic=lambda:future)):
            self.assertTrue((await self.send('after')).success)
        self.assertEqual([b.get('msg_id') for _,b in self.a.wire],['msg',None])
        self.assertEqual(self.used(),1)

    async def test_keyboard_payload_is_preserved_without_reply_anchor(self):
        keyboard={'content':{'rows':[]}}
        result=await self.a._send_group_text('group','approval',reply_to='msg',
                                           keyboard=SimpleNamespace(to_dict=lambda:keyboard))
        self.assertTrue(result.success)
        self.assertEqual(self.a.wire[0][1]['keyboard'],keyboard)
        self.assertIsNone(self.a.wire[0][1].get('msg_id'))

    async def test_retained_result_can_send_without_fresh_passive_anchor(self):
        self.a._last_msg_id.clear();self.a._qq_group_reply_seen.clear()
        key=self.a._qq_group_results_store.enqueue('group','historic','text',{'text':'retained'})
        result=await group_results._deliver(self.a,self.a._qq_group_results_store.get(key),None)
        self.assertTrue(result.success)
        self.assertIsNone(self.a.wire[0][1].get('msg_id'))

    async def test_exhausted_passive_quota_does_not_block_active_or_reset_counter(self):
        ledger=self.a._qq_reply_ledger
        for n in range(5):
            ledger.reserve('fixture-bot','msg','groups','group',str(n),'final')
            ledger.finish('fixture-bot','msg',str(n),response={'id':str(n)})
        self.assertTrue((await self.send('final')).success)
        self.assertEqual(self.used(),5)
        self.assertIsNone(self.a.wire[0][1].get('msg_id'))

    async def test_missing_ack_is_uncertain(self):
        self.a.missing_ack=True
        self.assertFalse((await self.send('answer')).success)
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'uncertain':1})
        self.assertEqual(len(self.a.wire),1)

    async def test_duplicate_final_is_not_resent_and_prior_budget_is_preserved(self):
        ledger=self.a._qq_reply_ledger
        ledger.reserve('fixture-bot','msg','groups','group','prior','final')
        ledger.finish('fixture-bot','msg','prior',response={'id':'old-ack'})
        results=await asyncio.gather(self.send('answer'),self.send('answer'))
        self.assertTrue(any(r.success for r in results))
        self.assertTrue((await self.send('answer')).success)
        self.assertEqual(len(self.a.wire),1)
        self.assertEqual(self.used(),1)

    async def test_wrong_group_anchor_is_rejected_before_proactive(self):
        r=await self.a.send('other','private result',reply_to='msg',metadata={'notify':True})
        self.assertFalse(r.success)
        self.assertEqual(self.a.wire,[])

    async def test_proactive_without_anchor_and_c2c_event_paths_are_preserved(self):
        self.assertTrue((await self.send('standalone',None)).success)
        self.a._qq_reply_ledger.remember('fixture-bot','dm','c2c','user','user',time.time())
        self.assertTrue((await self.a.send('user','private',reply_to='dm')).success)
        await self.a._api_request('POST','/v2/groups/group/messages',{'event_id':'event','content':'interaction'})
        self.assertIsNone(self.a.wire[0][1].get('msg_id'))
        self.assertEqual(self.a.wire[1][1]['msg_id'],'dm')
        self.assertEqual(self.a.wire[2][1]['event_id'],'event')


class PacingTests(unittest.TestCase):
    def test_rate_reservations_are_shared_by_adapters_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.sqlite3'
            first=reply_budget.Ledger(path);second=reply_budget.Ledger(path)
            with patch.object(reply_budget.time,'time',return_value=1000):
                self.assertEqual(first.group_send_delay('bot','group'),0)
                self.assertAlmostEqual(second.group_send_delay('bot','group'),3.1)
                self.assertAlmostEqual(second.group_send_delay('bot','other'),2.1)
            with patch.object(reply_budget.time,'time',return_value=1003.1):
                self.assertEqual(second.group_send_delay('bot','group'),0)
            with patch.object(reply_budget.time,'time',return_value=1003.2):
                restarted=reply_budget.Ledger(path)
                self.assertAlmostEqual(restarted.group_send_delay('bot','group'),3.0)


if __name__=='__main__':
    unittest.main()
