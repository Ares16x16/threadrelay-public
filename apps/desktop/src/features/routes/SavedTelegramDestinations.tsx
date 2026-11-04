import {useCallback,useEffect,useRef,useState} from 'react';
import type {AccountSnapshot,DestinationRequest,DestinationSnapshot,SavedTelegramDestination,SavedTelegramDestinations} from '../../lib/contracts';
import type {WorkerClient} from '../../lib/useWorker';
import {useResource} from '../../lib/useResource';
import {LoadState} from '../common';
import {DestinationPicker,type DestinationBrowserClient,type DestinationChoice} from './DestinationPicker';

export type SavedDestinationsClient={querySavedTelegramDestinations:()=>Promise<SavedTelegramDestinations>};
export type SavedDestinationsManagerClient=SavedDestinationsClient&DestinationBrowserClient&Pick<WorkerClient,'verifyDestination'|'queryDestination'|'saveTelegramDestination'>;
const kinds={saved_messages:'Saved Messages',private:'Private chat',group:'Group',channel:'Channel'};
const connected=(account:AccountSnapshot|null,authoritative:boolean)=>!!(authoritative&&account?.account_id&&account.auth_state==='ready'&&account.network_state==='online'&&!account.revocation_pending);
const sameSender=(left:DestinationSnapshot['sender'],right:DestinationSnapshot['sender'])=>left.kind===right.kind&&left.id===right.id;

/** Sending selectors read only the deliberately saved set, never discovery. */
export function SavedDestinationPicker({worker,account,authoritative,selectedId,disabled,onChoose,refreshKey=0,compact=false,onAvailabilityChange}:{worker:SavedDestinationsClient;account:AccountSnapshot|null;authoritative:boolean;selectedId:string;disabled:boolean;onChoose:(value:SavedTelegramDestination|null)=>void;refreshKey?:number;compact?:boolean;onAvailabilityChange?:(hasSavedChats:boolean)=>void}) {
 const load=useCallback(()=>worker.querySavedTelegramDestinations(),[worker,account?.account_id,account?.epoch,refreshKey]),data=useResource(load);
 const choose=useRef(onChoose);choose.current=onChoose;
 const ready=connected(account,authoritative)&&!data.loading&&!data.error;
 const items=data.value?.items.filter(item=>item.account_id===account?.account_id&&item.account_epoch===account?.epoch)??[];
 const availability=useRef(onAvailabilityChange);availability.current=onAvailabilityChange;
 useEffect(()=>{if(data.value&&!data.loading&&!data.error)availability.current?.(items.length>0);},[data.revision,data.loading,data.error,items.length]);
 useEffect(()=>{if(selectedId&&(!connected(account,authoritative)||data.error||data.value!==null&&(data.loading||!items.some(item=>item.chat_id===selectedId))))choose.current(null);},[selectedId,ready,data.revision,account?.account_id,account?.epoch]);
 return <section aria-label="Saved destination picker">{!compact&&<><h3>Choose a saved chat</h3><p>Only chats you explicitly verified and saved are offered here. Saving a chat does not send a message.</p></>}
 <button type="button" disabled={data.loading||disabled} onClick={()=>void data.refresh()}>Refresh saved chats</button>
 <LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>
 {!data.loading&&!data.error&&items.map(item=><button key={item.chat_id} type="button" className="list-row" aria-label={`Choose saved ${item.title||'Unnamed chat'} · ${item.chat_id}`} aria-pressed={selectedId===item.chat_id} disabled={!ready||disabled} onClick={()=>choose.current(item)}><strong>{item.title||'Unnamed chat'}</strong><span>{kinds[item.kind]} · {item.chat_id}</span><span>Sender {item.sender.kind} · {item.sender.id}</span></button>)}
 {!data.loading&&!data.error&&!items.length&&<p>No saved chats for this account and connection. Find, verify and save a chat in Saved Telegram destinations first.</p>}
 </section>;
}

/** Discovery is preparation only. One explicit Save verifies then persists identity. */
export function SavedTelegramDestinationsPane({worker,account,authoritative,onSaved}:{worker:SavedDestinationsManagerClient;account:AccountSnapshot|null;authoritative:boolean;onSaved?:()=>void|Promise<void>}) {
 const load=useCallback(()=>worker.querySavedTelegramDestinations(),[worker,account?.account_id,account?.epoch]),saved=useResource(load);
 type Selection={item:DestinationChoice;account:string;epoch:number};
 const [selected,setSelected]=useState<Selection|null>(null),[phase,setPhase]=useState<'idle'|'verifying'|'saving'>('idle'),[message,setMessage]=useState(''),[savedSelection,setSavedSelection]=useState(false);
 const mounted=useRef(false),intent=useRef(0),lock=useRef<number|null>(null),current=useRef({account,authoritative});current.current={account,authoritative};
 const waiting=useRef<{timer:ReturnType<typeof setTimeout>;resolve:()=>void}|null>(null);
 const reading=useRef<{cancel:()=>void}|null>(null),timedOut=Symbol('verification timeout');
 const cancelWait=()=>{const pending=waiting.current;if(pending){waiting.current=null;clearTimeout(pending.timer);pending.resolve();}reading.current?.cancel();};
 const bounded=<T,>(operation:Promise<T>,deadline:number)=>new Promise<T>((resolve,reject)=>{
  let settled=false;
  const finish=(complete:()=>void)=>{if(settled)return;settled=true;clearTimeout(timer);if(reading.current===handle)reading.current=null;complete();};
  const handle={cancel:()=>finish(()=>reject(new Error('Selection changed')))};
  const timer=setTimeout(()=>finish(()=>reject(timedOut)),Math.max(0,deadline-Date.now()));
  reading.current=handle;operation.then(value=>finish(()=>resolve(value)),error=>finish(()=>reject(error)));
 });
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;intent.current++;cancelWait();};},[]);
 const active=connected(account,authoritative),pending=phase!=='idle';
 useEffect(()=>{intent.current++;lock.current=null;cancelWait();setSelected(null);setPhase('idle');setMessage('');setSavedSelection(false);},[worker,account?.account_id,account?.epoch,active]);
 const choice=active&&selected&&selected.account===account?.account_id&&selected.epoch===account?.epoch?selected:null;
 const live=(ticket:number,binding:Selection)=>mounted.current&&ticket===intent.current&&connected(current.current.account,current.current.authoritative)&&current.current.account?.account_id===binding.account&&current.current.account?.epoch===binding.epoch;
 const wait=()=>new Promise<void>(resolve=>{waiting.current={resolve,timer:setTimeout(()=>{waiting.current=null;resolve();},500)};});
 const choose=(item:DestinationChoice)=>{if(!active||!account?.account_id||lock.current!==null||!item.can_verify||!item.sender)return;intent.current++;setSelected({item,account:account.account_id,epoch:account.epoch});setMessage('');setSavedSelection(false);};
 const save=async()=>{
  if(!choice||!choice.item.sender||lock.current!==null||savedSelection)return;
  const binding=choice,ticket=++intent.current,deadline=Date.now()+30000;lock.current=ticket;setPhase('verifying');setMessage('Checking this chat and your posting permission…');
  const request:DestinationRequest={chat_id:binding.item.chat_id,kind:binding.item.kind==='private'||binding.item.kind==='saved_messages'?'private':'group',sender:binding.item.kind==='private'||binding.item.kind==='saved_messages'?null:binding.item.sender};
  const verified=(snapshot:DestinationSnapshot|null)=>!!snapshot&&snapshot.account_id===binding.account&&snapshot.chat_id===binding.item.chat_id&&sameSender(snapshot.sender,binding.item.sender!)&&snapshot.permissions.can_send&&!snapshot.paid_required;
  try{
   try{await bounded(worker.verifyDestination(request),deadline);}
   catch(error){if(live(ticket,binding))setMessage(error===timedOut?'Chat verification timed out. Check Accounts, choose the chat again, then save. No chat was saved.':'Could not verify this chat. Check Accounts, choose the chat again, then save. No chat was saved.');return;}
   let verifiedSnapshot:DestinationSnapshot|null=null;
   for(let checks=0;checks<60&&live(ticket,binding)&&Date.now()<deadline;checks++){
    let result:Awaited<ReturnType<typeof worker.queryDestination>>;
    try{result=await bounded(worker.queryDestination(),deadline);}
    catch(error){if(live(ticket,binding))setMessage(error===timedOut?'Chat verification timed out. Check Accounts, choose the chat again, then save. No chat was saved.':'Could not verify this chat. Check Accounts, choose the chat again, then save. No chat was saved.');return;}
    if(!live(ticket,binding))return;
    if(Date.now()>=deadline)break;
    if(result.state==='pending'){await wait();continue;}
    if(result.state==='verified'&&verified(result.snapshot)){verifiedSnapshot=result.snapshot;break;}
    setMessage('Could not verify this chat and personal sender, or free posting is unavailable. Choose the chat again and check Accounts. No chat was saved.');return;
   }
   if(!live(ticket,binding))return;
   if(!verifiedSnapshot){setMessage('Chat verification timed out. Check Accounts, choose the chat again, then save. No chat was saved.');return;}
   setPhase('saving');setMessage('Saving this chat…');
   let reconciled=false;
   try{await worker.saveTelegramDestination({chat_id:binding.item.chat_id,account_id:binding.account,account_epoch:binding.epoch});}
   catch{
    if(!live(ticket,binding))return;
    // A lost ACK is resolved by one read of the persisted exact identity.
    // This never replays Save, verification, authentication or a send.
    const result=await saved.refresh();
    if(!live(ticket,binding))return;
    reconciled=!!result?.items.some(item=>item.account_id===binding.account&&item.account_epoch===binding.epoch&&item.chat_id===binding.item.chat_id&&sameSender(item.sender,binding.item.sender!)&&item.title===verifiedSnapshot.title&&item.kind===binding.item.kind);
    if(!reconciled){setMessage('Saving the chat was not confirmed. Refresh saved destinations, then choose the chat again before retrying.');return;}
   }
   if(!live(ticket,binding))return;
   setSavedSelection(true);setMessage('Chat saved. You can now choose it in a route.');
   // Refreshing other views is separate from the acknowledged write.
   let refreshed=reconciled||await saved.refresh()!==null;
   if(!live(ticket,binding))return;
   try{await onSaved?.();}catch{refreshed=false;}
   if(live(ticket,binding)&&!refreshed)setMessage('Chat saved. Refresh saved destinations to update the available choices.');
  }finally{if(lock.current===ticket){lock.current=null;if(mounted.current)setPhase('idle');}}
 };
 const items=saved.value?.items.filter(item=>item.account_id===account?.account_id&&item.account_epoch===account?.epoch)??[];
 return <section className="panel" aria-label="Saved Telegram destinations"><div className="section-heading"><h2>Saved Telegram destinations</h2><button type="button" disabled={saved.loading||pending} onClick={()=>void saved.refresh()}>Refresh saved destinations</button></div>
 <p>Choose a chat and save it. ThreadRelay checks its identity and posting permission for you.</p>
 <LoadState loading={saved.loading} error={saved.error} hasValue={false} retry={()=>void saved.refresh()}/>
 {!saved.loading&&!saved.error&&items.map(item=><div key={item.chat_id} className="identity"><p>Saved: {item.title||'Unnamed chat'} · {item.chat_id}</p><details><summary>Recipient details</summary><p>{kinds[item.kind]} · sender {item.sender.kind} {item.sender.id} · account {item.account_id} · epoch {item.account_epoch}</p></details></div>)}
 {!saved.loading&&!saved.error&&!items.length&&<p>No saved chats for this account yet.</p>}
 <details open={!items.length}><summary>Find and save a chat</summary>
 {account?<DestinationPicker worker={worker} account={account} authoritative={authoritative} selectedId={choice?.item.chat_id??''} disabled={pending} onChoose={choose}/>:<p>Sign in to Telegram in Accounts before finding a chat.</p>}
 {choice&&<div className="identity"><p>Selected chat: {choice.item.title||'Unnamed chat'} · {choice.item.chat_id}</p><details><summary>Selected sender</summary><p>Sender {choice.item.sender?.kind} · {choice.item.sender?.id} · account {choice.account} · epoch {choice.epoch}</p></details></div>}
 <div className="actions"><button type="button" className="primary" disabled={!choice||pending||savedSelection} onClick={()=>void save()}>{phase==='verifying'?'Checking chat…':phase==='saving'?'Saving chat…':'Save selected chat'}</button></div>
 </details>{message&&<p role="status">{message}</p>}
 <details><summary>How saved chats are used</summary><p>Routes and sending plans use only chats you explicitly save. Saving does not send a message. Current permissions and free posting are checked again before sending.</p></details></section>;
}
