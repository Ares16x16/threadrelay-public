import {useCallback,useEffect,useState} from 'react';
import {useResource} from '../../lib/useResource';
import {LoadState} from '../common';
import type {TelegramDestinationCandidate,TelegramDestinations} from '../../lib/contracts';

export type DestinationChoice=TelegramDestinationCandidate;
export type DestinationPage=TelegramDestinations;
export type DestinationBrowserClient={refreshTelegramDestinations:(request:{load_more:boolean})=>Promise<void>;queryTelegramDestinations:(request:{cursor:string|null;limit:number;search:string})=>Promise<DestinationPage>};
type Account={account_id:string|null;epoch:number;auth_state:string;network_state:string;revocation_pending:boolean};
const kinds={saved_messages:'Saved Messages',private:'Private chat',group:'Group',channel:'Channel',secret:'Secret chat · unsupported',unsupported:'Unsupported chat'};
const failureStages={load:'loading chat lists',chats:'reading chat lists',chat:'reading chat details',member:'checking a chat’s sender'};
const failureReasons={timeout:'No response arrived in time',invalid_response:'The response could not be verified',request_unavailable:'The request could not be opened'};
export function DestinationPicker({worker,account,authoritative,selectedId,disabled,onChoose}:{worker:DestinationBrowserClient;account:Account;authoritative:boolean;selectedId:string;disabled:boolean;onChoose:(value:DestinationChoice)=>void}) {
 const [search,setSearch]=useState(''),[cursors,setCursors]=useState<Array<string|null>>([null]),[revision,setRevision]=useState(0),[pending,setPending]=useState(false),[message,setMessage]=useState('');
 const cursor=cursors[cursors.length-1];
 const load=useCallback(()=>worker.queryTelegramDestinations({cursor,limit:30,search}),[worker,cursor,search,revision,account.account_id,account.epoch,account.auth_state,account.network_state,account.revocation_pending,authoritative]);
 const data=useResource(load),page=data.value;
 const connected=authoritative&&account.auth_state==='ready'&&account.network_state==='online'&&!account.revocation_pending&&!!account.account_id;
 const current=connected&&page?.account_id===account.account_id&&page.account_epoch===account.epoch;
 useEffect(()=>{setCursors([null]);setSearch('');setMessage('');},[account.account_id,account.epoch]);
 useEffect(()=>{if(!current||page?.state!=='loading'||data.error||data.loading)return;const timer=setTimeout(()=>void data.refresh({background:true}),500);return()=>clearTimeout(timer);},[current,page?.state,data.error,data.loading,data.revision,data.refresh]);
 const refresh=async(loadMore:boolean)=>{
  if(disabled||pending||!connected)return;
  setPending(true);setMessage('');
  try{await worker.refreshTelegramDestinations({load_more:loadMore});setCursors([null]);setRevision(value=>value+1);}
  catch{setMessage('Chat names could not be refreshed. Check the Telegram connection in Accounts, then try again.');setRevision(value=>value+1);}
  finally{setPending(false);}
 };
 const selectable=current&&!data.error&&!data.loading&&!pending&&!disabled&&['ready','loading'].includes(page?.state??'');
 return <section aria-label="Telegram destination picker"><h3>Choose a Telegram destination</h3><p>Browse your chat and group names, then choose one to verify its sender and posting permission. Loading names does not send messages.</p>
 {!connected&&<p className="notice">Sign in to Telegram in Accounts and wait for an online connection, then refresh your chats here.</p>}
 <div className="actions"><button type="button" disabled={!connected||pending||disabled||page?.state==='loading'} onClick={()=>void refresh(false)}>{pending?'Refreshing chats…':'Refresh chats'}</button>{current&&page?.state==='loading'&&<span role="status">Loading chat names…</span>}</div>
 <label>Search loaded chats<input maxLength={200} value={search} onChange={event=>{setSearch(event.target.value);setCursors([null]);}} placeholder="Name or chat ID"/></label>
 <p className="muted">Search covers the chats loaded so far. Load more to include older and archived chats.</p>
 <LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>
 {message&&<p role="status" className="notice">{message}</p>}
 {current&&page?.state==='blocked'&&<p className="notice">{page.blocked_reason==='refresh_required'?'The account changed. Refresh the chat list.':'The Telegram connection is not ready. Check Accounts, then refresh the chat list.'}</p>}
 {current&&page?.state==='failed'&&<p className="notice">Chat loading stopped before it finished. {page.failure_stage&&page.failure_reason&&<span>Step: {failureStages[page.failure_stage]}. {failureReasons[page.failure_reason]}. </span>}Refresh chats to try again; existing names are not posting permission.</p>}
 {current&&!data.error&&page?.items.map(item=><button key={item.chat_id} type="button" className="list-row" aria-label={'Choose '+(item.title||'unnamed chat')+' · '+item.chat_id} aria-pressed={selectedId===item.chat_id} disabled={!selectable||!item.can_verify||!item.sender} onClick={()=>onChoose(item)}><strong>{item.title||'Unnamed chat'}</strong><span>{kinds[item.kind]} · {item.chat_id}</span>{!item.can_verify&&<span className="muted">Its current sender or chat type cannot be verified here.</span>}</button>)}
 {current&&page?.state==='idle'&&<p>Refresh chats to load your Telegram destinations.</p>}
 {current&&page?.state==='ready'&&!page.items.length&&<p>{search?'No loaded chats match this search.':'No chat names loaded yet.'}</p>}
 {current&&page?.capped&&<p className="notice">The native chat limit was reached. This list is incomplete; it is not a complete account inventory.</p>}
 {current&&page?.exhausted&&<p className="muted">All available chats loaded.</p>}
 <div className="actions"><button type="button" disabled={disabled||data.loading||cursors.length<2} onClick={()=>setCursors(values=>values.slice(0,-1))}>Previous chats page</button><button type="button" disabled={disabled||data.loading||!current||!page?.next_cursor} onClick={()=>{if(page?.next_cursor)setCursors(values=>[...values,page.next_cursor]);}}>Next chats page</button><button type="button" disabled={disabled||pending||!current||!page?.can_load_more||page.state==='loading'} onClick={()=>void refresh(true)}>Load more chats</button></div>
 </section>;
}
