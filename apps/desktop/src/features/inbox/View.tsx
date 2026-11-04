import { useCallback, useEffect, useRef, useState } from 'react';
import type { AccountSnapshot, ReviewItem, ReviewApprovalRequest, BulkReviewRequest, BulkDismissRequest } from '../../lib/contracts';
import type { WorkerClient } from '../../lib/useWorker';
import { useResource } from '../../lib/useResource';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { LoadState } from '../common';
import { CollectedPosts } from './CollectedPosts';

type Worker = Pick<WorkerClient,'queryReview'|'queryAccount'|'approveReview'|'dismissReview'> & Partial<Pick<WorkerClient,'bulkApproveReview'|'bulkDismissReview'|'queryCollection'|'testSample'>>;
export function ReviewDetail({item,account,authoritative,approve,dismiss,present=true,revalidate,onCloseStale}: {item:ReviewItem;account:AccountSnapshot|null;authoritative:boolean;approve:(request:ReviewApprovalRequest)=>Promise<void>|void;dismiss:()=>Promise<void>|void;present?:boolean;revalidate?:()=>Promise<boolean>;onCloseStale?:()=>void}) {
  const frozen=useRef<{item:ReviewItem;account:AccountSnapshot|null}|null>(null);
  const [fingerprint,setFingerprint] = useState<string|null>(null), [pending,setPending] = useState(false), [opening,setOpening] = useState(false), [checking,setChecking] = useState(false), [invalidated,setInvalidated] = useState(false), [result,setResult] = useState('');
  const mounted=useRef(true),openLock=useRef(false);useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  const {proposal:p,classification} = item; const d=p.destination; const a=account;
  const current = JSON.stringify({item,account});
  const currentRef=useRef(current);currentRef.current=current;
  const valid = !!(authoritative && a && a.auth_state==='ready' && !a.revocation_pending && a.account_id===p.route.account_id && a.epoch===p.route.account_epoch && d && d.account_id===a.account_id && d.chat_id===p.route.destination_id && JSON.stringify(d.sender)===JSON.stringify(p.route.sender) && !d.paid_required && d.permissions.can_send && p.plan_id && p.destination_version && p.route_refs?.length && classification && classification.post_id===p.post.post_id && classification.topic_results.every(r=>item.topics.some(t=>t.id===r.topic_id)) && item.outgoing_text===p.post.permalink);
  const changedNow = fingerprint !== null && (fingerprint !== current || !present);
  useEffect(()=>{if(changedNow)setInvalidated(true);},[changedNow]);
  const changed = fingerprint !== null && (changedNow || invalidated);
  const openReview=()=>{if(!valid||pending||openLock.current)return;const snapshot=current;const open=()=>{frozen.current=structuredClone({item,account});setInvalidated(false);setFingerprint(snapshot);};if(!revalidate){open();return;}openLock.current=true;setOpening(true);setResult('');void revalidate().then(ok=>{if(!mounted.current)return;if(ok && currentRef.current===snapshot)open();else setResult('Details changed — review again.');}).catch(()=>{if(mounted.current)setResult('Current details could not be confirmed.');}).finally(()=>{openLock.current=false;if(mounted.current)setOpening(false);});};
  const doApprove = async () => {
    if(!valid || changed || pending || !d || !p.plan_id || !p.destination_version) return;
    const request:ReviewApprovalRequest = {proposal:p,disclosure:{plan_id:p.plan_id,destination_version:p.destination_version,destination_id:d.chat_id,title:d.title,audience:d.audience,source_visibility:p.post.visibility,sender_kind:d.sender.kind,sender_id:d.sender.id}};
    setPending(true); setResult('');
    try {await approve(request); setFingerprint(null); setResult('Approval staged. Sending still requires a Running session.');}
    catch {setResult('Approval could not be staged. Review current details again.');setFingerprint(null);}
    finally {setPending(false);}
  };
  return <div className="review-detail"><h2>Source</h2><p><strong>{p.post.author}</strong> · {p.post.visibility} visibility</p><p className="preserve">{p.post.text}</p>{p.post.quote && <blockquote><strong>Quoted from {p.post.quote.author}</strong><p className="preserve">{p.post.quote.text}</p></blockquote>}
    <p>Canonical link: <span className="wrap">{p.post.permalink}</span></p>{p.post.quality_flags.length>0 && <p className="warning">Source quality: {p.post.quality_flags.map(f=>f.code).join(', ')}</p>}
    <h2>Classification evidence</h2>{classification ? <><p>Model: {classification.model_version}</p><ul>{classification.topic_results.map(t=><li key={t.topic_id}>{t.topic_id} · {item.topics.find(topic=>topic.id===t.topic_id)?.description ?? 'Definition unavailable'}: {t.abstained?'Abstained':t.match?'Match':'No match'}</li>)}</ul></> : <p>Classification evidence is unavailable; approval is disabled.</p>}
    <h2>Delivery identity</h2><div className="identity"><p>Verified account: {a?.auth_state==='ready' && a.account_id ? `${a.display_name || 'Personal account'} · ${a.account_id}` : 'Not currently verified'}</p><p>Effective sender: {d ? `${d.sender.kind==='chat'?'Channel':'User'} · ${d.sender.id}` : 'Unverified'}</p><p>Destination: {d ? `${d.title} · ${d.chat_id}` : 'Unverified'}</p><p>{d ? `${d.audience[0].toUpperCase()+d.audience.slice(1)} audience` : 'Audience unknown'}</p><p>{d?.paid_required ? 'Paid messaging is not supported; delivery is blocked.' : d?.permissions.can_send ? 'Posting permission recorded; zero-paid status recorded.' : 'Posting permission not verified.'}</p></div>
    <h2>Outgoing message</h2><p className="outgoing preserve">{item.outgoing_text}</p><p className="muted">This is the exact message preview from the current local formatter; source text and quotes are not copied.</p>
    <div className="actions"><button className="primary" disabled={!valid||pending||opening} onClick={openReview}>{opening?'Checking…':'Review approval'}</button><button disabled={!authoritative||pending} onClick={()=>void dismiss()}>Dismiss</button></div>
    {!valid && <p className="notice">Current identity, classification, destination or routing authority is incomplete. Refresh and verify before approval.</p>}{result && <p role="status">{result}</p>}
    {fingerprint!==null && <ConfirmDialog title="Confirm review" confirmLabel={pending?'Staging…':'Approve for session'} disabled={!valid||changed||pending} onCancel={()=>{setFingerprint(null);if(!present)onCloseStale?.();}} onConfirm={()=>void doApprove()}>
      {changed && <p className="warning" role="alert">Details changed — review again.</p>}{!authoritative && !changed && <p className="notice">Current authority is being checked or is unavailable.</p>}{revalidate && <button type="button" disabled={checking||pending} onClick={()=>{setChecking(true);void revalidate().finally(()=>{if(mounted.current)setChecking(false);});}}>{checking?'Checking…':'Check current details'}</button>}
      <p>Account: {frozen.current?.account?.display_name || 'Personal account'} · {frozen.current?.account?.account_id} · epoch {frozen.current?.account?.epoch}</p><p>Sender: {frozen.current?.item.proposal.destination?.sender.kind} · {frozen.current?.item.proposal.destination?.sender.id}</p><p>Destination: {frozen.current?.item.proposal.destination?.title} · {frozen.current?.item.proposal.destination?.chat_id} · {frozen.current?.item.proposal.destination?.audience} audience</p><p>Source: {frozen.current?.item.proposal.post.visibility}; routes: {frozen.current?.item.proposal.route_refs?.map(r=>`${r.route_id} v${r.route_version}`).join(', ')}</p><p className="outgoing preserve">{frozen.current?.item.outgoing_text}</p><p>Approval is staged. Sending requires an explicit Running session.</p>
    </ConfirmDialog>}
  </div>;
}

export function InboxView({worker,navigate}: {worker:Worker;navigate:()=>void}) {
  const load = useCallback(()=>worker.queryReview(),[worker]); const loadAccount=useCallback(()=>worker.queryAccount(),[worker]);
  // These bounded reads are observational only. They never poll TDLib, reconnect,
  // or repeat an approval/dismissal; the worker owner remains the receive owner.
  const data=useResource(load,5000), account=useResource(loadAccount,5000);
  const [bulkChecked,setBulkChecked]=useState<Record<string,string>>({}),[bulkPending,setBulkPending]=useState(false);
  const [selected,setSelected]=useState<string|null>(null), [message,setMessage]=useState('');
  const selectedRef=useRef(selected);selectedRef.current=selected;
  const lastSelected=useRef<{id:string;item:ReviewItem}|null>(null);
  const fresh=data.value?.find(i=>i.proposal.plan_id===selected);
  const chosen=fresh ?? (selected && lastSelected.current?.id===selected ? lastSelected.current.item : undefined);
  const authoritative=!!(fresh && data.value && account.value && !data.error&&!data.loading&&!account.error&&!account.loading);
  const refreshBoth=()=>Promise.all([data.refresh(),account.refresh()]);
  const revalidate=async()=>{
    if(!chosen || !selected || !account.value)return false;
    const id=selected, expected=JSON.stringify({item:chosen,account:account.value.snapshot});
    const [reviews,latestAccount]=await refreshBoth();
    const latest=reviews?.find(i=>i.proposal.plan_id===id);
    return !!(selectedRef.current===id && latest && latestAccount && JSON.stringify({item:latest,account:latestAccount.snapshot})===expected);
  };
  const bulkRows=(data.value??[]).slice(0,20);
  const bulkFingerprint=(item:ReviewItem)=>JSON.stringify({item,account:account.value?.snapshot});
  const bulkSelected=bulkRows.filter(item=>item.proposal.plan_id&&bulkChecked[item.proposal.plan_id]===bulkFingerprint(item));
  const bulk=async(dismiss:boolean)=>{
    if(bulkPending||!bulkSelected.length||!account.value)return;
    setBulkPending(true);setMessage('');
    try {
      const expected=structuredClone(bulkSelected), expectedAccount=JSON.stringify(account.value.snapshot);
      const [reviews,currentAccount]=await refreshBoth();
      if(!reviews||!currentAccount||JSON.stringify(currentAccount.snapshot)!==expectedAccount||expected.some(item=>JSON.stringify(reviews.find(row=>row.proposal.plan_id===item.proposal.plan_id))!==JSON.stringify(item)))throw new Error('Changed');
      if(dismiss){if(!worker.bulkDismissReview)throw new Error('Unavailable');await worker.bulkDismissReview({items:expected.map(item=>({plan_id:item.proposal.plan_id!})) as BulkDismissRequest['items']});}
      else {if(!worker.bulkApproveReview)throw new Error('Unavailable');const items=expected.map(item=>{const p=item.proposal,d=p.destination!;return {proposal:p,disclosure:{plan_id:p.plan_id!,destination_version:p.destination_version!,destination_id:d.chat_id,title:d.title,audience:d.audience,source_visibility:p.post.visibility,sender_kind:d.sender.kind,sender_id:d.sender.id}};});const result=await worker.bulkApproveReview({items:items as BulkReviewRequest['items']});setMessage(`${result.results.filter(v=>v.status==='staged').length} approvals staged; ${result.results.filter(v=>v.status==='rejected').length} rejected. Only a Running session may send.`);}
      setBulkChecked({});await refreshBoth();
    } catch {setMessage('Bulk action not confirmed or details changed. Review each item again.');setBulkChecked({});}
    finally {setBulkPending(false);}
  };
  return <><header className="page-header"><div><h1 tabIndex={-1}>Review inbox</h1><p>Inspect collected topic results and separately review delivery proposals.</p></div><button disabled={data.loading||account.loading} onClick={()=>void refreshBoth()}>Check review status</button></header>
    {worker.queryCollection&&<CollectedPosts worker={worker as Worker & Pick<WorkerClient,'queryCollection'>} />}
    <h2>Delivery approvals</h2><p>Delivery proposals require a verified account and route. Collection results above remain available without either.</p>
    <LoadState loading={data.loading} error={data.error} hasValue={data.value!==null} retry={()=>void data.refresh()} />
    <LoadState loading={account.loading} error={account.error} hasValue={account.value!==null} retry={()=>void account.refresh()} />
    {!!bulkRows.length && worker.bulkApproveReview && <section className="panel"><h2>Bulk review (up to 20 items)</h2><p>Select posts to stage or dismiss. If an item’s details change, select it again.</p>{bulkRows.map(item=>{const p=item.proposal,d=p.destination,id=p.plan_id;if(!id||!d)return null;const valid=!!(account.value?.snapshot.auth_state==='ready'&&!account.value.snapshot.revocation_pending&&account.value.snapshot.account_id===p.route.account_id&&account.value.snapshot.epoch===p.route.account_epoch&&d.permissions.can_send&&!d.paid_required&&p.destination_version&&item.classification&&item.outgoing_text===p.post.permalink);return <details key={id}><summary>{p.post.author} → {d.title} · {d.audience} audience</summary><p>{p.post.text}</p>{p.post.quote&&<blockquote>Quoted from {p.post.quote.author}: {p.post.quote.text}</blockquote>}<p>Classification: {item.classification?.model_version} · {item.classification?.topic_results.map(t=>`${t.topic_id}: ${t.abstained?'Abstained':t.match?'Match':'No match'}`).join('; ')}</p><p>Source visibility: {p.post.visibility}; quality: {p.post.quality_flags.map(f=>f.code).join(', ')||'no flagged issue'}.</p><p>Account {p.route.account_id} · epoch {p.route.account_epoch}; sender {d.sender.kind} · {d.sender.id}; destination {d.title} · {d.chat_id} · {d.audience} audience.</p><p>Routes: {p.route_refs?.map(r=>`${r.route_id} v${r.route_version}`).join(', ')}. Topics: {p.labels.join(', ')}.</p><p>Exact outgoing message: {item.outgoing_text}</p><label className="check"><input type="checkbox" disabled={!valid||bulkPending||data.loading||account.loading||!!data.error||!!account.error} checked={bulkChecked[id]===bulkFingerprint(item)} onChange={e=>setBulkChecked(v=>({...v,[id]:e.target.checked?bulkFingerprint(item):''}))} /> Select post</label></details>;})}<div className="actions"><button disabled={!bulkSelected.length||bulkPending||data.loading||account.loading} onClick={()=>void bulk(false)}>Stage {bulkSelected.length} selected approval{bulkSelected.length===1?'':'s'}</button><button disabled={!bulkSelected.length||bulkPending||!worker.bulkDismissReview} onClick={()=>void bulk(true)}>Dismiss {bulkSelected.length} selected item{bulkSelected.length===1?'':'s'}</button></div></section>}
    {data.value?.length===0 && !chosen ? <section className="panel"><EmptyState title="Nothing needs review" action={<button onClick={navigate}>Dashboard</button>}>New review proposals appear here when local classification and routing are available.</EmptyState></section> : <div className="inbox-layout"><section className="panel"><h2>Needs review</h2>{data.value?.map(i=><button className="list-row" key={i.proposal.plan_id} aria-pressed={selected===i.proposal.plan_id} onClick={()=>{if(i.proposal.plan_id){lastSelected.current={id:i.proposal.plan_id,item:i};setSelected(i.proposal.plan_id);}}}><strong>{i.proposal.post.author}</strong><span>{i.proposal.post.text.slice(0,140)}</span><small>{i.proposal.labels.join(', ')} · {i.proposal.post.visibility} · 1 destination</small></button>)}</section><section className="panel">{chosen ? <ReviewDetail item={chosen} account={account.value?.snapshot ?? null} present={!!fresh} authoritative={authoritative} revalidate={revalidate} onCloseStale={()=>setSelected(null)} approve={async req=>{try{await worker.approveReview(req);}catch(error){await refreshBoth();throw error;}setSelected(null);await refreshBoth();}} dismiss={async()=>{if(!chosen.proposal.plan_id)return;try{await worker.dismissReview({plan_id:chosen.proposal.plan_id});setSelected(null);setMessage('Review dismissed.');await data.refresh();}catch{setMessage('Review could not be dismissed.');}}} /> : <EmptyState title="Select a review">Choose a row to inspect its source and destination.</EmptyState>}</section></div>}{message && <p role="status">{message}</p>}
  </>;
}
