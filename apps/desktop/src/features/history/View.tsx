import { useCallback, useState } from 'react';
import type { DeliveryDraft } from '../../lib/contracts';
import { DeliveryComposer } from '../sending/Composer';
import { DeliveryJobsView } from '../sending/Jobs';
import type { DeliveryClient } from '../sending/types';
import type { WorkerClient } from '../../lib/useWorker';
import { useResource } from '../../lib/useResource';
import { StatusBadge } from '../../components/StatusBadge';
import { ArchivedDeliveries } from './Deliveries';
import { ArchivePosts, ArchiveRuns } from './Archive';
import type { ArchiveClient } from './archiveTypes';
import { EmptyState } from '../../components/EmptyState';
import { LoadState } from '../common';
function DeliveryHistory({worker}:{worker:Pick<WorkerClient,'queryHistory'>}) {
  const load=useCallback(()=>worker.queryHistory(),[worker]);const data=useResource(load);
  return <><h2>Delivery outcomes</h2><LoadState loading={data.loading} error={data.error} hasValue={data.value!==null} retry={()=>void data.refresh()} />
    {data.value?.length===0 && <section className="panel"><EmptyState title="No retained deliveries">No delivery outcomes have been recorded. Earlier deleted data cannot be reconstructed.</EmptyState></section>}
    {!!data.value?.length && <section className="panel table-wrap"><table><caption>Delivery outcomes</caption><thead><tr><th>Source</th><th>Destination ID</th><th>Sender</th><th>State</th><th>Attempts and details</th></tr></thead><tbody>{data.value.map(({delivery:d,attempts})=><tr key={d.id}><td>{d.post_id}</td><td>{d.destination_id}</td><td>{d.sender.kind} · {d.sender.id}</td><td><StatusBadge value={d.state} />{d.state==='unknown' && <p className="warning">May already have been delivered. Automatic retry is blocked.</p>}{d.state==='sending' && <p className="muted">Initiated; not yet confirmed.</p>}</td><td><details><summary>{attempts.length} attempt{attempts.length===1?'':'s'}</summary><p>Account {d.account_id} · epoch {d.account_epoch}</p><p>Routes: {d.route_refs.map(r=>`${r.route_id} v${r.route_version}`).join(', ')}</p>{attempts.map(a=><div key={a.attempt_id}><p>Attempt {a.attempt_id} · {a.state}</p><p>Correlation {a.request_extra}</p><p>Temporary ID: {a.temporary_message_id ?? '—'} · Final ID: {a.final_message_id ?? '—'}</p></div>)}</details></td></tr>)}</tbody></table></section>}
  </>;
}

export function HistoryView({worker,onOpenAccounts,onBusy}:{onBusy?:(busy:boolean)=>void;onOpenAccounts?:()=>void;worker:Pick<WorkerClient,'queryHistory'>&Partial<ArchiveClient>&Partial<DeliveryClient>&Partial<Pick<WorkerClient,'queryArchiveDeliveries'|'queryArchiveDelivery'>>}) {
 const archive=typeof worker.queryArchivePosts==='function'&&typeof worker.queryArchiveRevision==='function'&&typeof worker.queryArchiveRuns==='function'&&typeof worker.queryArchiveRun==='function';
 const [tab,setTab]=useState<'Posts'|'Model runs'|'Deliveries'>(archive?'Posts':'Deliveries'),[revision,setRevision]=useState<string|null>(null),[sources,setSources]=useState<string[]|null>(null),[draft,setDraft]=useState<DeliveryDraft|undefined>(undefined),[jobRevision,setJobRevision]=useState(0),[sendNotice,setSendNotice]=useState(''),[deliveryBusy,setDeliveryBusy]=useState(false),[outcomeRevision,setOutcomeRevision]=useState(0);
 const sending=typeof worker.queryDeliveryWorkspace==='function'&&typeof worker.configureDeliveryDraft==='function'&&typeof worker.previewDeliveryDraft==='function'&&typeof worker.approveDeliveryDraft==='function'&&typeof worker.queryDeliveryJobs==='function';
 const prepare=(ids:string[])=>{setDraft(undefined);setSources(ids);};
 const tabs=['Posts','Model runs','Deliveries'] as const;
 return <><header className="page-header"><div><h1 tabIndex={-1}>History</h1><p>Permanent captured posts, model runs and delivery outcomes.</p></div></header><div role="tablist" aria-label="History sections" className="actions history-tabs">{tabs.map((name,index)=><button key={name} id={`history-tab-${index}`} role="tab" disabled={deliveryBusy} aria-controls={`history-panel-${index}`} tabIndex={tab===name?0:-1} aria-selected={tab===name} onKeyDown={event=>{if(deliveryBusy)return;const next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;if(next!==null){event.preventDefault();setTab(tabs[next]);(event.currentTarget.parentElement?.querySelectorAll('[role=tab]')[next] as HTMLElement|undefined)?.focus();}}} onClick={()=>setTab(name)}>{name}</button>)}</div>
 <div role="tabpanel" id={`history-panel-${tabs.indexOf(tab)}`} aria-labelledby={`history-tab-${tabs.indexOf(tab)}`}>
 {tab==='Posts'&&(archive?<ArchivePosts worker={worker as ArchiveClient} onRuns={id=>{setRevision(id);setTab('Model runs');}} onPrepare={prepare}/>:<p>Archive access is unavailable. Reopen the current desktop build.</p>)}
 {tab==='Model runs'&&(archive?<ArchiveRuns key={revision??'all'} worker={worker as ArchiveClient} revisionId={revision} onClear={()=>setRevision(null)} onPrepare={prepare}/>:<p>Model run history is unavailable in this build.</p>)}
 {tab==='Deliveries'&&<>{sendNotice&&<p role="status">{sendNotice}</p>}{sending&&<DeliveryJobsView worker={worker as DeliveryClient} revision={jobRevision} sendDisabled={sources!==null} onOpenAccounts={onOpenAccounts} onBusy={value=>{setDeliveryBusy(value);onBusy?.(value);}} onOutcome={()=>setOutcomeRevision(value=>value+1)} onEdit={value=>{setDraft(value);setSources(value.revision_ids);}} onNewRule={()=>prepare([])}/>}{worker.queryArchiveDeliveries&&worker.queryArchiveDelivery?<ArchivedDeliveries key={outcomeRevision} worker={worker as Pick<WorkerClient,'queryArchiveDeliveries'|'queryArchiveDelivery'>}/>:<DeliveryHistory key={outcomeRevision} worker={worker}/>}</>}
 </div>
 {sources!==null&&(sending?<DeliveryComposer key={draft?`${draft.id}-${draft.version}`:sources.join(',')} worker={worker as DeliveryClient} revisionIds={sources} initialDraft={draft} onClose={()=>setSources(null)} onApproved={()=>{setSources(null);setTab('Deliveries');setSendNotice('Plan saved. Review its status below before sending.');setJobRevision(old=>old+1);}}/>:<p role="status">Sending workspace unavailable in this build. Your selected revisions remain in History.</p>)}
 </>;
}
