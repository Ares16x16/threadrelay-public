import { useCallback, useState } from 'react';
import type { WorkerClient } from '../../lib/useWorker';
import { useDetailHeading } from './useDetailHeading';
import { useResource } from '../../lib/useResource';
import { StatusBadge } from '../../components/StatusBadge';
import { LoadState } from '../common';
import { RecordFields } from './Archive';
type Client=Pick<WorkerClient,'queryArchiveDeliveries'|'queryArchiveDelivery'>;
function OutcomeDetail({worker,id}:{worker:Client;id:string}) {
 const heading=useDetailHeading();
 const load=useCallback(()=>worker.queryArchiveDelivery(id),[worker,id]),data=useResource(load);
 return <section className="panel" aria-label="Delivery outcome details"><h2 tabIndex={-1} ref={heading}>Delivery outcome details</h2><LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>{!data.loading&&!data.error&&data.value&&<><RecordFields value={{...data.value.delivery}}/><h3>Recorded attempts</h3>{!data.value.attempts.length&&<p>No attempts were recorded.</p>}{data.value.attempts.map(attempt=><article key={attempt.attempt_id}><h4>Attempt {attempt.attempt_id}</h4><RecordFields value={{...attempt}}/></article>)}</>}</section>;
}
export function ArchivedDeliveries({worker}:{worker:Client}) {
 const [text,setText]=useState(''),[search,setSearch]=useState(''),[cursors,setCursors]=useState<Array<string|null>>([null]),[detail,setDetail]=useState<string|null>(null),cursor=cursors[cursors.length-1];
 const load=useCallback(()=>worker.queryArchiveDeliveries({cursor,limit:20,search}),[worker,cursor,search]),data=useResource(load);
 return <><section className="panel"><div className="section-heading"><h2>Delivery outcomes</h2><button disabled={data.loading} onClick={()=>void data.refresh()}>Refresh delivery outcomes</button></div><p>Permanent outcomes from this app. Missing older data is not reconstructed.</p><form onSubmit={event=>{event.preventDefault();setSearch(text.trim());setCursors([null]);setDetail(null);}}><label>Search delivery outcomes<input maxLength={200} value={text} onChange={event=>setText(event.target.value)}/></label><button>Search deliveries</button></form><LoadState loading={data.loading} error={data.error} hasValue={false} retry={()=>void data.refresh()}/>
 {!data.loading&&!data.error&&data.value&&<>{!data.value.items.length&&<p>No recorded delivery outcomes match this search.</p>}{data.value.items.map(({delivery,attempt_count,created_at,updated_at})=><article key={delivery.id}><h3>Source {delivery.post_id}</h3><StatusBadge value={delivery.state}/><p>Destination {delivery.destination_id} · sender {delivery.sender.kind} {delivery.sender.id}</p><p className="muted">Created {created_at} · Last updated {updated_at}</p><p>{attempt_count} attempt{attempt_count===1?'':'s'}</p>{delivery.state==='unknown'&&<p className="warning">May already have been delivered. Automatic retry is blocked.</p>}{delivery.state==='sending'&&<p className="muted">Initiated; not yet confirmed.</p>}<button onClick={()=>setDetail(delivery.id)}>Inspect delivery {delivery.id}</button></article>)}</>}
 <div className="actions"><button disabled={data.loading||cursors.length<2} onClick={()=>setCursors(old=>old.slice(0,-1))}>Previous deliveries page</button><button disabled={data.loading||!!data.error||!data.value?.next_cursor} onClick={()=>{if(data.value?.next_cursor)setCursors(old=>[...old,data.value!.next_cursor]);}}>Next deliveries page</button></div></section>{detail&&<OutcomeDetail key={detail} worker={worker} id={detail}/>}</>;
}
