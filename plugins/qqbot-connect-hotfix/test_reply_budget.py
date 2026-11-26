"""Native QQ sender regression with fake network; temporary owner-local state only."""
import asyncio
import hashlib
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('qq_budget_test', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec); sys.modules[spec.name] = plugin; spec.loader.exec_module(plugin)
from qq_budget_test import reply_budget as budget, group_results as outbox
from gateway.config import PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter


class Adapter(QQAdapter):
    async def _api_request(self, method, path, body=None, *args, **kwargs):
        self.wire.append((path, dict(body)))
        if getattr(self,'expired_anchor',None) and body.get('msg_id')==self.expired_anchor:
            raise RuntimeError('QQ Bot API error [400]: msg_id已过期')
        if self.failure:
            raise RuntimeError(self.failure)
        await asyncio.sleep(0)
        return {'id': 'ack-' + str(len(self.wire))}

    async def _upload_local_file(self, chat_type, chat, path, file_type, file_name):
        self.uploads += 1
        return Path(path).name, {'file_info': Path(path).name}

    async def _upload_media(self, *args, **kwargs):
        return {'file_info': 'remote-image'}

    async def _process_message_background(self, event, session_key):
        self.processed += 1

budget.patch_reply_budget(Adapter)
outbox.patch_group_results(Adapter)


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = Adapter(PlatformConfig(extra={'app_id':'fixture-bot','client_secret':'fixture-secret'}))
        self.a._running = True; self.a._ws = SimpleNamespace(closed=False)
        self.a._markdown_support = False
        self.a.set_authorization_check(lambda *args: True)
        self.a._chat_type_map = {'group':'group', 'user':'c2c'}
        self.a._qq_reply_ledger = budget.Ledger(self.root/'ledger.sqlite3')
        self.a._qq_group_results_store = outbox.Store(self.root/'outbox')
        self.a._qq_group_reply_seen = {'group':('msg',time.time())}
        self.a.wire=[]; self.a.failure=None; self.a.processed=0; self.a.uploads=0
        self.remember('msg')

    def remember(self, msg, scene='group', chat='group', sender='user', stamp=None):
        self.a._qq_reply_ledger.remember('fixture-bot', msg, scene, chat, sender, stamp or time.time())

    async def text(self, text, final=False, msg='msg', chat='group', progress=False):
        return await self.a.send(chat,text,reply_to=msg,metadata={'notify':final,'_interim_send':progress})

    async def test_progress_coalesces_and_reserves_final_text_and_file(self):
        await self.text('working',progress=True)
        for i in range(10):await self.text('working '+str(i),progress=True)
        for i in range(5):await self.text('tool '+str(i))
        self.assertEqual(len(self.a.wire),3)
        self.assertTrue((await self.text('final',final=True)).success)
        f=self.root/'answer.xlsx';f.write_bytes(b'fixture')
        self.assertTrue((await self.a.send_document('group',str(f),reply_to='msg',metadata={'notify':True})).success)
        self.assertEqual(len(self.a.wire),5)
        self.assertEqual([b['msg_seq'] for _,b in self.a.wire],[1,2,3,4,5])
        self.assertTrue(all(b['msg_id']=='msg' for _,b in self.a.wire))
        self.assertFalse((await self.text('sixth',final=True)).success)
        self.assertEqual(len(self.a.wire),5)

    async def test_atomic_across_senders_and_restart_duplicate_event(self):
        results=await asyncio.gather(*(self.text('part '+str(i),final=True) for i in range(12)))
        self.assertEqual(sum(r.success for r in results),5)
        self.a._qq_reply_ledger=budget.Ledger(self.root/'ledger.sqlite3')
        self.remember('msg')
        self.assertFalse((await self.text('after restart',final=True)).success)
        self.assertEqual(len(self.a.wire),5)

    async def test_confirmed_replay_is_idempotent_and_ambiguity_stays_charged(self):
        self.assertTrue((await self.text('same',final=True)).success)
        self.assertTrue((await self.text('same',final=True)).success)
        self.assertEqual(len(self.a.wire),1)
        self.a.failure='QQ Bot API timeout'
        self.assertFalse((await self.text('unknown',final=True)).success)
        self.a.failure=None
        self.assertFalse((await self.text('unknown',final=True)).success)
        self.assertEqual(len(self.a.wire),2)
        self.assertEqual(self.a._qq_group_results_store.counts('group')['uncertain'],1)

    async def test_group_private_replies_share_budget_and_wrong_target_rejected(self):
        token=budget.PRIORITY.set('final')
        try:
            for i in range(5):
                route='/v2/groups/group/messages' if i%2 else '/v2/users/user/messages'
                await self.a._api_request('POST',route,{'msg_id':'msg','content':str(i)})
            with self.assertRaises(budget.BudgetDenied):await self.a._api_request('POST','/v2/users/user/messages',{'msg_id':'msg','content':'six'})
            with self.assertRaises(budget.BudgetDenied):await self.a._api_request('POST','/v2/users/stranger/messages',{'msg_id':'msg','content':'wrong'})
        finally:budget.PRIORITY.reset(token)
        self.assertEqual(len(self.a.wire),5)

    async def test_stream_updates_do_not_spend_new_slots(self):
        self.remember('dm','c2c','user')
        path='/v2/users/user/stream_messages'
        first=await self.a._api_request('POST',path,{'msg_id':'dm','msg_seq':900,'index':0,'input_state':1,'content_raw':'a'})
        for i in range(1,9):
            await self.a._api_request('POST',path,{'msg_id':'dm','msg_seq':900,'stream_msg_id':first['id'],'index':i,'input_state':10 if i==8 else 1,'content_raw':'a'*i})
        self.assertEqual({b['msg_seq'] for _,b in self.a.wire},{1})
        token=budget.PRIORITY.set('final')
        try:
            for i in range(3):await self.a._api_request('POST','/v2/users/user/messages',{'msg_id':'dm','content':str(i)})
            with self.assertRaises(budget.BudgetDenied):await self.a._api_request('POST',path,{'msg_id':'dm','msg_seq':901,'index':0,'content_raw':'new'})
        finally:budget.PRIORITY.reset(token)

    async def test_server_budget_rejection_stops_network_retries_and_retains_result(self):
        self.a.failure='QQ Bot API error [400]: 回复消息失败，被动回复时间或者次数超过限制'
        r=await self.text('final',final=True)
        self.assertFalse(r.success);self.assertFalse(r.retryable)
        self.a.failure=None
        self.assertFalse((await self.text('other final',final=True)).success)
        self.assertEqual(len(self.a.wire),1)
        self.assertEqual(self.a._qq_group_results_store.counts('group')['pending'],2)

    async def test_fresh_message_flushes_pending_without_recomputing(self):
        for i in range(7):await self.text('result '+str(i),final=True)
        self.assertEqual(len(self.a.wire),5)
        self.remember('new-msg')
        event=SimpleNamespace(source=SimpleNamespace(chat_type='group',chat_id='group',user_id='user'),message_id='new-msg',timestamp=datetime.now(timezone.utc),internal=False,text='领取结果',is_command=lambda:False)
        await self.a._process_message_background(event,'fixture-session')
        self.assertEqual(self.a.processed,1)  # Native lifecycle remains responsible for cleanup.
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'delivered':7})
        self.assertEqual(len(self.a.wire),7)  # This fixture's lifecycle does not emit the notice.
        self.assertTrue(all(body['msg_id']=='new-msg' for _,body in self.a.wire[5:]))

    async def test_sixty_minute_expiry_and_ten_minute_reply(self):
        self.remember('ten-minutes',stamp=time.time()-600)
        self.assertTrue((await self.text('still valid',final=True,msg='ten-minutes')).success)
        self.remember('old',stamp=time.time()-3601)
        self.assertFalse((await self.text('expired',final=True,msg='old')).success)
        self.assertEqual(len(self.a.wire),1)

    async def test_file_and_local_image_retain_after_budget_exhaustion(self):
        for i in range(5):await self.text(str(i),final=True)
        p=self.root/'picture.png';p.write_bytes(b'fixture')
        r=await self.a.send_image_file('group',str(p),reply_to='msg',metadata={'notify':True})
        self.assertFalse(r.success)
        row=self.a._qq_group_results_store.pending('group')[0]
        copied=Path(json.loads(row['payload'])['path']);p.unlink();self.assertEqual(copied.read_bytes(),b'fixture')
        self.assertEqual(len(self.a.wire),5)

    async def test_http_rejection_releases_reservation_but_preserves_unique_sequence(self):
        self.a.failure='QQ Bot API error [400]: invalid markdown'
        token=budget.PRIORITY.set('final')
        try:
            with self.assertRaises(RuntimeError):await self.a._api_request('POST','/v2/groups/group/messages',{'msg_id':'msg','content':'x'})
            self.a.failure=None
            await self.a._api_request('POST','/v2/groups/group/messages',{'msg_id':'msg','content':'x'})
        finally:budget.PRIORITY.reset(token)
        self.assertEqual([b['msg_seq'] for _,b in self.a.wire],[1,2])

    async def test_identical_adjacent_chunks_are_distinct_but_full_retry_is_not(self):
        self.a.truncate_message=lambda text, size: [text[:4],text[4:]]
        text='samesame'
        self.assertTrue((await self.text(text,final=True)).success)
        self.assertEqual(len(self.a.wire),2)
        self.assertTrue((await self.text(text,final=True)).success)
        self.assertEqual(len(self.a.wire),2)
        self.remember('dm','c2c','user')
        self.assertTrue((await self.text(text,final=True,msg='dm',chat='user')).success)
        self.assertEqual(len(self.a.wire),4)
        self.assertTrue(all(body['msg_id']=='dm' for _,body in self.a.wire[2:]))

    async def test_raw_ingress_does_not_replay_results_before_authorization(self):
        self.a._qq_group_results_store.enqueue('group','old','text',{'text':'retained'})
        event=SimpleNamespace(source=SimpleNamespace(chat_type='group',chat_id='group',user_id='user'),
                              message_id='incoming',timestamp=datetime.now(timezone.utc),internal=False)
        self.a._message_handler=None
        await self.a.handle_message(event)
        self.assertEqual(self.a.wire,[])
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'pending':1})

    async def test_group_media_share_text_budget_including_remote_image(self):
        for i in range(3):await self.text(str(i))
        r=await self.a.send_image('group','https://example.invalid/image.png',reply_to='msg')
        self.assertFalse(r.success)
        self.assertEqual(len(self.a.wire),3)
        self.assertTrue((await self.text('answer',final=True)).success)
        self.remember('next')
        await outbox._flush(self.a,'group','next')
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'delivered':5})

    async def test_unknown_stream_id_cannot_reuse_an_ordinary_message_slot(self):
        first=await self.text('ordinary',final=True)
        with self.assertRaises(budget.BudgetDenied):
            await self.a._api_request('POST','/v2/users/user/stream_messages',
                                     {'msg_id':'msg','stream_msg_id':first.message_id,'index':1})
        self.assertEqual(len(self.a.wire),1)

    async def test_recovery_requires_explicit_gateway_authorization(self):
        self.a._qq_group_results_store.enqueue('group','old','text',{'text':'retained'})
        event=SimpleNamespace(source=SimpleNamespace(chat_type='group',chat_id='group',user_id='user'),
                              message_id='msg',timestamp=datetime.now(timezone.utc),internal=False,
                              text='领取结果',is_command=lambda:False)
        for decision in [False,None,'true']:
            self.a.set_authorization_check(lambda *args: decision)
            await self.a._process_message_background(event,'session')
        self.assertEqual(self.a.wire,[])
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'pending':1})

    async def test_existing_windows_pending_text_is_not_duplicated_on_upgrade(self):
        payload={'text':'retained','part':0}
        key=hashlib.sha256(json.dumps(['group','msg','text',payload],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.a._qq_group_results_store.connect() as db:
            db.execute("INSERT INTO results VALUES(?,?,?,?,?,'pending',?,NULL,NULL)",
                       (key,'group','msg','text',json.dumps(payload),time.time()))
        self.assertTrue((await self.text('retained',final=True)).success)
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'delivered':1})

    async def test_missing_attachment_still_reports_failure(self):
        token=outbox._TURN.set({'chat':'group','origin':'msg','command':False})
        try:
            with self.assertRaises(FileNotFoundError):
                await self.a.send_document('group',str(self.root/'missing.xlsx'),reply_to='msg',metadata={'notify':True})
            await self.a._notify_media_delivery_failure('group',str(self.root/'missing.xlsx'),metadata={'notify':True})
        finally:outbox._TURN.reset(token)
        self.assertEqual(len(self.a.wire),1)
        self.assertIn("Couldn't deliver",self.a.wire[0][1]['content'])

    async def test_retained_attachment_does_not_enqueue_a_false_failure_notice(self):
        for i in range(5):await self.text(str(i),final=True)
        f=self.root/'retained.xlsx';f.write_bytes(b'fixture')
        token=outbox._TURN.set({'chat':'group','origin':'msg','command':False})
        try:
            self.assertFalse((await self.a.send_document('group',str(f),reply_to='msg',metadata={'notify':True})).success)
            await self.a._notify_media_delivery_failure('group',str(f),metadata={'notify':True})
        finally:outbox._TURN.reset(token)
        self.assertEqual(self.a._qq_group_results_store.counts('group'),{'delivered':5,'pending':1})

    async def test_results_command_releases_real_hermes_session_guard(self):
        from gateway.platforms.base import MessageEvent
        from gateway.config import Platform
        from gateway.session import SessionSource
        class NativeLifecycle(QQAdapter):
            _api_request = Adapter._api_request.__wrapped__
        budget.patch_reply_budget(NativeLifecycle)
        outbox.patch_group_results(NativeLifecycle)
        a=NativeLifecycle(PlatformConfig(extra={'app_id':'fixture-bot','client_secret':'fixture-secret'}))
        a._running=True;a._ws=SimpleNamespace(closed=False);a._markdown_support=False
        a._chat_type_map={'group':'group'};a.wire=[];a.failure=None
        a._qq_reply_ledger=self.a._qq_reply_ledger
        a._qq_group_results_store=self.a._qq_group_results_store
        a.set_authorization_check(lambda *args:True)
        called=[]
        async def model(event):called.append(event);return 'should not invoke model'
        a.set_message_handler(model)
        event=MessageEvent(text='领取结果',source=SessionSource(platform=Platform.QQBOT,chat_id='group',chat_type='group',user_id='user'),message_id='msg',timestamp=datetime.now(timezone.utc))
        a._start_session_processing(event,'lifecycle-test')
        await a._session_tasks['lifecycle-test']
        self.assertNotIn('lifecycle-test',a._active_sessions)
        self.assertEqual(called,[])
        def unavailable(*args):raise OSError('fixture database unavailable')
        a._qq_group_results_store.pending=unavailable
        a._start_session_processing(event,'lifecycle-io-error')
        await a._session_tasks['lifecycle-io-error']
        self.assertNotIn('lifecycle-io-error',a._active_sessions)
        self.assertEqual(called,[])

    async def test_existing_expired_anchor_fallback_is_not_reattached_by_budget(self):
        from qq_budget_test.outbound import patch_expired_reply_fallback
        class ExpiryAdapter(Adapter):
            _send_group_text=QQAdapter._send_group_text
            _send_c2c_text=QQAdapter._send_c2c_text
        patch_expired_reply_fallback(ExpiryAdapter)
        a=ExpiryAdapter(PlatformConfig(extra={'app_id':'fixture-bot','client_secret':'fixture-secret'}))
        a.__dict__.update(self.a.__dict__);a.expired_anchor='msg'
        self.assertTrue((await a.send('group','answer',reply_to='msg',metadata={'notify':True})).success)
        self.assertEqual([b.get('msg_id') for _,b in a.wire],['msg',None])

if __name__=='__main__':unittest.main()
