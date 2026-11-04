import { useCallback, useEffect, useRef, useState } from 'react';
import type { AccountSnapshot, SavedTelegramDestination, TelegramTestRequest, TelegramTestState } from '../../lib/contracts';
import { useResource } from '../../lib/useResource';
import {SavedDestinationPicker,type SavedDestinationsClient} from './SavedTelegramDestinations';

export type TelegramTestClient=SavedDestinationsClient&{
 queryTelegramTest:()=>Promise<TelegramTestState>;
 sendTelegramTest:(request:TelegramTestRequest)=>Promise<void>;
};
type Selection={account:string;epoch:number;item:SavedTelegramDestination};
const busyStates=new Set(['verifying','sending','closing']);
const messages:Record<TelegramTestState['state'],string>={
 idle:'Choose the Test group to send one connection check.',
 verifying:'Verifying the Test group, current account and posting permission…',
 sending:'Awaiting Telegram confirmation. A request or temporary ID is not confirmed delivery.',
 closing:'Closing the test connection. Final test status is not yet confirmed.',
 sent:'Test confirmed by Telegram; connection closed cleanly.',
 failed:'Test failed. No retry was sent. Review its status and Telegram before any further action.',
 unknown:'Outcome unknown. Do not resend: check Telegram before taking any further action.',
};
export function TelegramTestPanel({worker,account,authoritative}:{worker:TelegramTestClient;account:AccountSnapshot|null;authoritative:boolean}) {
 const load=useCallback(()=>worker.queryTelegramTest(),[worker]),data=useResource(load);
 const [chosen,setChosen]=useState<Selection|null>(null),[message,setMessage]=useState(''),[pending,setPending]=useState(false),[awaiting,setAwaiting]=useState(false),[attempted,setAttempted]=useState(false);
 const mounted=useRef(false),locked=useRef(false);
 const connected=authoritative&&account?.auth_state==='ready'&&account.network_state==='online'&&!account.revocation_pending&&!!account.account_id;
 const current=useRef({account,connected});current.current={account,connected};
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
 useEffect(()=>{setChosen(null);setMessage('');},[account?.account_id,account?.epoch]);
 useEffect(()=>{if(data.value&&data.value.state!=='idle')setAwaiting(false);},[data.revision]);
 const state=data.value?.state;
 const busy=pending||awaiting||busyStates.has(state??'');
 useEffect(()=>{if(!busy)return;const timer=setInterval(()=>void data.refresh({background:true}),1000);return()=>clearInterval(timer);},[busy,data.refresh]);
 const selected=chosen&&connected&&chosen.account===account?.account_id&&chosen.epoch===account.epoch?chosen:null;
 const allowed=!!selected&&state==='idle'&&!pending&&!awaiting&&!attempted&&!data.loading&&!data.error;
 const choose=(item:SavedTelegramDestination|null)=>{
  setChosen(null);if(!item)return;
  if(!connected||!account?.account_id||busy||attempted||state!=='idle'||item.account_id!==account.account_id||item.account_epoch!==account.epoch)return;
  if(item.kind!=='group'||item.title!=='Test'||item.sender?.kind!=='user'||item.sender.id!==account.account_id){setMessage('Choose a group named exactly “Test” using your personal account. Save the exact Test group first. Other destinations cannot receive this test.');return;}
  setMessage('');setChosen({account:account.account_id,epoch:account.epoch,item});
 };
 const send=async()=>{
  const binding=selected,live=current.current;
  if(!allowed||locked.current||!binding||!live.connected||live.account?.account_id!==binding.account||live.account.epoch!==binding.epoch)return;
  locked.current=true;setPending(true);setAttempted(true);setAwaiting(true);setMessage('');
  try{await worker.sendTelegramTest({chat_id:binding.item.chat_id,account_id:binding.account,account_epoch:binding.epoch});}
  catch{if(mounted.current){setAwaiting(false);setMessage('The test request was not confirmed. Read its status and check Telegram; no send will be retried automatically.');}}
  finally{locked.current=false;if(mounted.current){setPending(false);void data.refresh({background:true});}}
 };
 const priorAccount=!!(data.value?.account_id&&(data.value.account_id!==account?.account_id||data.value.account_epoch!==account?.epoch));
 return <section className="panel" aria-label="Test Telegram connection"><h2>Test Telegram connection</h2>
  <p>Send one harmless connection check to a saved group named exactly <strong>Test</strong>. The backend creates fixed test text with a unique marker; it contains no collected post content.</p>
  {!connected&&<p className="notice">Sign in to Telegram in Accounts and wait for an online connection before choosing the Test group.</p>}
  {account&&<SavedDestinationPicker worker={worker} account={account} authoritative={authoritative} selectedId={selected?.item.chat_id??''} disabled={busy||attempted||!!state&&state!=='idle'} onChoose={choose}/>}
  {selected&&<div className="identity"><p>{selected.item.title} · {selected.item.chat_id}</p><p>Account {selected.account} · epoch {selected.epoch}</p><p>Exactly one fixed test message. This is an explicit send to this group.</p></div>}
  {message&&<p className="notice" role="status">{message}</p>}
  {data.error&&<p className="error" role="alert">Test status could not be read. No delivery outcome is assumed.</p>}
  <p role="status">{pending?'Requesting one test message…':awaiting?'Test requested. Checking its current status…':state?messages[state]:data.loading?'Reading local test status…':'Test status is unconfirmed.'}</p>
  {priorAccount&&<p className="warning">This recorded test belongs to an earlier account or account epoch. It is not proof for the current account.</p>}
  {data.value?.chat_id&&<p className="muted">Recorded test destination: {data.value.chat_id} · account {data.value.account_id} · epoch {data.value.account_epoch}</p>}
  {data.value?.text&&<div className="outgoing"><h3>Test message</h3><p className="preserve">{data.value.text}</p></div>}
  {state==='sent'&&data.value?.final_message_id&&<p>Final message ID: {data.value.final_message_id}</p>}
  {state==='sent'&&<p className="muted">The test connection is closed. You can sign in again in Accounts if needed.</p>}
  <div className="actions"><button className="primary" disabled={!allowed} onClick={()=>void send()}>Send one test message</button>{(data.error||attempted||state&&state!=='idle')&&<button disabled={data.loading} onClick={()=>void data.refresh({background:true})}>Read test status</button>}</div>
  <p className="muted">This test does not enable forwarding, scheduled deliveries or Auto-send. Those require their own verified setup and approval.</p>
 </section>;
}
