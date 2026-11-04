import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { HistoryView } from './View';
const target={account_id:'123',account_epoch:2,destination_id:'123',destination_version:4,sender:{kind:'user',id:'123'}};
const template={id:'url',version:1,name:'Post URL',body:'{{url}}',readonly:true,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z'};
const draft={source_policy:'captured',id:'draft1',version:1,revision_ids:['revision1'],target,template_id:'url',template_version:1,timing:{mode:'now',timezone:'Asia/Hong_Kong'},created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z'};
const preview={source_policy:'captured',draft_id:'draft1',version:1,preview_hash:'a'.repeat(64),items:[{revision_id:'revision1',post_id:'post1',url:'https://www.threads.com/@alice/post/post1',text:'https://www.threads.com/@alice/post/post1',due_at:'2026-11-01T01:00:00Z',missing_fields:[],source_eligible:true,blocked_reasons:[]}],target,destination:{account_id:'123',chat_id:'123',title:'Saved Messages',audience:'private',permissions:{can_send:true,can_select_sender:false},sender:target.sender,paid_required:false},topic_bindings:[],timing:draft.timing,approvable:true,blocked_reasons:[]};
const job={source_policy:'captured',id:'job1',version:3,kind:'job',draft_id:'draft1',draft_version:1,revision_id:'revision1',post_id:'post1',text:preview.items[0].text,target,due_at:preview.items[0].due_at,timing:draft.timing,status:'waiting',blocked_reasons:[],requires_approval:false,rule_id:null,delivery_id:null,attempt_id:null,created_at:draft.created_at,updated_at:draft.updated_at};
function worker(){return {queryHistory:vi.fn().mockResolvedValue([]),queryArchivePosts:vi.fn().mockResolvedValue({items:[{revision_id:'revision1',post_id:'post1',permalink:preview.items[0].url,author:'alice',text_preview:'Saved source',observed_at:draft.created_at,visibility:'public',quality_flags:[],provenance:'captured'}],next_cursor:null}),queryArchiveRevision:vi.fn().mockResolvedValue({post_id:'post1',permalink:preview.items[0].url,author:'alice',text:'Saved source',content_hash:'a'.repeat(64),visibility:'public',quality_flags:[],observed_at:draft.created_at,source_session_id:'source1',quote:null}),queryArchiveRuns:vi.fn(),queryArchiveRun:vi.fn(),queryTopics:vi.fn().mockResolvedValue([{id:'love-and-dating',version:1,description:'Love and dating',positive_examples:[],negative_examples:[],exclusions:[],enabled:true}]),queryDeliveryWorkspace:vi.fn().mockResolvedValue({templates:[template],targets:[{target,destination:{account_id:'123',chat_id:'123',title:'Saved Messages',audience:'private',sender:target.sender,permissions:{can_send:true,can_select_sender:false},paid_required:false}}],drafts:[],setup_reasons:[]}),saveMessageTemplate:vi.fn().mockResolvedValue({...template,id:'custom',readonly:false}),configureDeliveryDraft:vi.fn().mockImplementation(async request=>({...draft,...request,id:'draft1',version:1})),previewDeliveryDraft:vi.fn().mockResolvedValue(preview),approveDeliveryDraft:vi.fn().mockResolvedValue({items:[job],next_cursor:null}),queryDeliveryJobs:vi.fn().mockResolvedValue({items:[],next_cursor:null}),pauseDeliveryJob:vi.fn().mockResolvedValue({...job,status:'paused',version:4}),resumeDeliveryJob:vi.fn().mockResolvedValue({...job,status:'waiting',version:5}),cancelDeliveryJob:vi.fn().mockResolvedValue({...job,status:'cancelled',version:4}),getDeliveryDraft:vi.fn().mockResolvedValue(draft)};}
async function compose(w=worker()) {render(<HistoryView worker={w}/>);fireEvent.click(await screen.findByLabelText('Select revision revision1'));fireEvent.click(screen.getByRole('button',{name:'Prepare send from selected posts'}));await screen.findByLabelText('Saved destination');return w;}

it('requires a fresh preview after outgoing text changes and saves its exact hash',async()=>{
 const w=await compose();expect(w.configureDeliveryDraft).not.toHaveBeenCalled();expect(w.approveDeliveryDraft).not.toHaveBeenCalled();expect(screen.getByLabelText('Message template')).toHaveValue('{{url}}');
 fireEvent.change(screen.getByLabelText('Saved destination'),{target:{value:'0'}});fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));
 const exact=await screen.findByRole('region',{name:'Exact message preview'});expect(within(exact).getByText(preview.items[0].text)).toBeVisible();
 expect(screen.getByRole('button',{name:'Save message plan'})).toBeEnabled();
 fireEvent.change(screen.getByLabelText('Message template'),{target:{value:'{{author}} — {{url}}'}});expect(screen.queryByRole('region',{name:'Exact message preview'})).not.toBeInTheDocument();expect(w.approveDeliveryDraft).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await screen.findByRole('region',{name:'Exact message preview'});fireEvent.click(screen.getByRole('button',{name:'Save message plan'}));
 await waitFor(()=>expect(w.approveDeliveryDraft).toHaveBeenCalledWith({draft_id:'draft1',version:1,preview_hash:'a'.repeat(64)}));expect(await screen.findByText(/Plan saved/)).toBeVisible();
});

it.each([
 ['now',{}, {mode:'now',timezone:'Asia/Hong_Kong'}],
 ['fixed',{}, {mode:'fixed',timezone:'Asia/Hong_Kong',start_at:null,interval_seconds:300}],
 ['random',{}, {mode:'random',timezone:'Asia/Hong_Kong',start_at:null,minimum_seconds:300,maximum_seconds:900}],
 ['at_time',{'Send at (Asia/Hong_Kong)':'2026-12-01T09:30'}, {mode:'at_time',timezone:'Asia/Hong_Kong',at:'2026-12-01T01:30:00.000Z'}],
 ['new_matching',{}, {mode:'new_matching',timezone:'Asia/Hong_Kong',topic_ids:['love-and-dating'],interval_seconds:300,delivery_mode:'review'}],
])('binds %s timing to the draft before preview',async(mode,fields,timing)=>{
 const w=await compose();fireEvent.change(screen.getByLabelText('Timing'),{target:{value:mode}});for(const[label,value]of Object.entries(fields))fireEvent.change(screen.getByLabelText(label),{target:{value}});
 if(mode==='new_matching')fireEvent.click(await screen.findByLabelText('Love and dating'));
 fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenCalledWith(expect.objectContaining({revision_ids:['revision1'],target:null,timing})));
});

it('keeps targetless previews blocked and rejects late preview authority after an edit',async()=>{
 const w=worker();w.queryDeliveryWorkspace.mockResolvedValue({templates:[template],targets:[],drafts:[],setup_reasons:['account_not_ready']});let finish!:(value:typeof preview)=>void;w.previewDeliveryDraft.mockImplementation(()=>new Promise(r=>{finish=r;}));
 await compose(w);expect(screen.getAllByText(/Connect Telegram in Accounts/)[0]).toBeVisible();fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.previewDeliveryDraft).toHaveBeenCalled());
 fireEvent.change(screen.getByLabelText('Message template'),{target:{value:'Changed {{url}}'}});await act(async()=>finish(preview));expect(screen.queryByRole('region',{name:'Exact message preview'})).not.toBeInTheDocument();expect(w.approveDeliveryDraft).not.toHaveBeenCalled();
});

it('uses exact job versions for durable pause/cancel and offers no blind retry for unknown outcomes',async()=>{
 const w=worker();w.queryDeliveryJobs.mockResolvedValue({items:[job,{...job,id:'job-unknown',status:'unknown',blocked_reasons:['unknown_outcome']}],next_cursor:null});render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));
 const waiting=await screen.findByRole('article',{name:'Delivery job job1'});fireEvent.click(within(waiting).getByRole('button',{name:'Pause job'}));await waitFor(()=>expect(w.pauseDeliveryJob).toHaveBeenCalledWith({job_id:'job1',version:3}));
 const unknown=screen.getByRole('article',{name:'Delivery job job-unknown'});expect(within(unknown).queryByRole('button',{name:/resume|retry/i})).not.toBeInTheDocument();expect(within(unknown).getByText(/May already have been delivered/)).toBeVisible();
});

it('starts a new-history rule without old sources and displays the exact automatic rule before consent',async()=>{
 const w=worker();w.previewDeliveryDraft.mockResolvedValue({...preview,items:[],topic_bindings:[{id:'love-and-dating',version:9,description:'Current relationship definition'}],timing:{mode:'new_matching',timezone:'Asia/Hong_Kong',topic_ids:['love-and-dating'],interval_seconds:300,delivery_mode:'automatic'},blocked_reasons:['automatic_evidence_unavailable']});
 render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));fireEvent.click(await screen.findByRole('button',{name:'Prepare new-history rule'}));await screen.findByLabelText('Saved destination');
 expect(screen.getByLabelText('Timing')).toHaveValue('new_matching');fireEvent.click(await screen.findByLabelText('Love and dating'));fireEvent.click(screen.getByLabelText('Automatically send future matching arrivals using this rule'));fireEvent.change(screen.getByLabelText('Saved destination'),{target:{value:'0'}});fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));
 const exact=await screen.findByRole('region',{name:'Exact message preview'});expect(within(exact).getByText(/Destination 123/)).toBeVisible();expect(within(exact).getByText(/300 seconds/)).toBeVisible();expect(within(exact).getByText(/Topics: love-and-dating/)).toBeVisible();expect(within(exact).getByText('{{url}}')).toBeVisible();expect(within(exact).getByText(/Current relationship definition · version 9/)).toBeVisible();expect(within(exact).getByText(/Saved Messages · private audience/)).toBeVisible();expect(within(exact).getByRole('button',{name:'Save message plan'})).toBeEnabled();expect(w.approveDeliveryDraft).not.toHaveBeenCalled();
});

it('opens a review-arrival draft with its saved source and requires fresh preview before approving',async()=>{
 const w=worker();w.queryDeliveryJobs.mockResolvedValue({items:[{...job,status:'draft',requires_approval:true}],next_cursor:null});render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));fireEvent.click(await screen.findByRole('button',{name:'Review job draft'}));
 await screen.findByLabelText('Saved destination');expect(w.getDeliveryDraft).toHaveBeenCalledWith('draft1');await waitFor(()=>expect(screen.getByLabelText('Saved destination')).toHaveValue('0'));expect(screen.queryByRole('button',{name:'Save message plan'})).not.toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenCalledWith(expect.objectContaining({id:'draft1',expected_version:1,revision_ids:['revision1']})));
});

it('resumes a paused job only explicitly and cancels a queued job with its current version',async()=>{
 const w=worker();w.queryDeliveryJobs.mockResolvedValue({items:[{...job,status:'paused'}, {...job,id:'job2',version:9}],next_cursor:null});render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));
 const paused=await screen.findByRole('article',{name:'Delivery job job1'});expect(w.resumeDeliveryJob).not.toHaveBeenCalled();fireEvent.click(within(paused).getByRole('button',{name:'Resume job'}));await waitFor(()=>expect(w.resumeDeliveryJob).toHaveBeenCalledWith({job_id:'job1',version:3}));
 await waitFor(()=>expect(screen.getByRole('article',{name:'Delivery job job2'})).toBeVisible());fireEvent.click(within(screen.getByRole('article',{name:'Delivery job job2'})).getByRole('button',{name:'Cancel job'}));await waitFor(()=>expect(w.cancelDeliveryJob).toHaveBeenCalledWith({job_id:'job2',version:9}));
});

it('keeps the returned draft version after an edit during saving without restoring the old preview',async()=>{
 const w=worker();let finish!:(value:typeof draft)=>void;w.configureDeliveryDraft.mockImplementationOnce(()=>new Promise(r=>{finish=r;}));await compose(w);
 fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenCalled());fireEvent.change(screen.getByLabelText('Message template'),{target:{value:'Changed {{url}}'}});
 await act(async()=>finish({...draft,version:7}));expect(w.previewDeliveryDraft).not.toHaveBeenCalled();expect(screen.queryByRole('region',{name:'Exact message preview'})).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenLastCalledWith(expect.objectContaining({id:'draft1',expected_version:7})));
});

it('can explicitly recheck a blocked job after setup changes without first pausing it',async()=>{
 const w=worker();w.queryDeliveryJobs.mockResolvedValue({items:[{...job,status:'blocked',blocked_reasons:['transport_offline']}],next_cursor:null});render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));
 const blocked=await screen.findByRole('article',{name:'Delivery job job1'});fireEvent.click(within(blocked).getByRole('button',{name:'Resume job'}));await waitFor(()=>expect(w.resumeDeliveryJob).toHaveBeenCalledWith({job_id:'job1',version:3}));
});

it('moves keyboard focus to the composer opened from a history selection',async()=>{
 await compose();expect(screen.getByRole('heading',{name:'Prepare send'})).toHaveFocus();
});

it('saves an explicitly named template even when its URL-only body is unchanged',async()=>{
 const w=await compose();fireEvent.change(screen.getByLabelText('Template name'),{target:{value:'My links'}});fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.saveMessageTemplate).toHaveBeenCalledWith({id:null,expected_version:null,name:'My links',body:'{{url}}'}));
});

it('uses captured provenance and shows recorded source metadata in the exact preview',async()=>{
 const w=worker();w.queryArchiveRevision.mockResolvedValue({post_id:'post1',permalink:preview.items[0].url,author:'alice',text:'Saved source',content_hash:'a'.repeat(64),visibility:'unknown',quality_flags:[{code:'visibility_unverified',detail:'No public audience badge was observed.'}],observed_at:draft.created_at,source_session_id:'source1',quote:null});
 await compose(w);await waitFor(()=>expect(w.queryArchiveRevision).toHaveBeenCalledWith('revision1'));fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenCalledWith(expect.objectContaining({source_policy:'captured'})));
 const exact=await screen.findByRole('region',{name:'Exact message preview'});expect(await within(exact).findByText('Recorded source visibility: unknown')).toBeVisible();expect(within(exact).getByText(/visibility_unverified: No public audience badge/)).toBeVisible();
});

it('uses captured provenance when reopening a legacy draft',async()=>{
 const w=worker();const {source_policy:unused,...legacy}=draft;void unused;w.getDeliveryDraft.mockResolvedValue(legacy);w.queryDeliveryJobs.mockResolvedValue({items:[{...job,status:'draft',requires_approval:true}],next_cursor:null});render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));fireEvent.click(await screen.findByRole('button',{name:'Review job draft'}));
 await screen.findByLabelText('Saved destination');fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenCalledWith(expect.objectContaining({source_policy:'captured'})));
});

it('loads more than eight selected sources within the worker queue budget and preserves selection order',async()=>{
 const w=worker(),ids=['rev-8','rev-1','rev-12','rev-2','rev-11','rev-3','rev-10','rev-4','rev-9','rev-5','rev-7','rev-6'];
 w.queryArchivePosts.mockResolvedValue({items:ids.map(revision_id=>({revision_id,post_id:`source-${revision_id}`,permalink:preview.items[0].url,author:'alice',text_preview:revision_id,observed_at:draft.created_at,visibility:'public',quality_flags:[],provenance:'captured'})),next_cursor:null});
 let activeReads=0,peakReads=0;const completed:string[]=[];
 w.queryArchiveRevision.mockImplementation(async(revision_id:string)=>{activeReads++;peakReads=Math.max(peakReads,activeReads);try{if(activeReads>3)throw new Error('worker queue capacity exceeded');await new Promise(resolve=>setTimeout(resolve,5));completed.push(revision_id);return {post_id:`source-${revision_id}`,permalink:preview.items[0].url,author:'alice',text:'Saved source',content_hash:'a'.repeat(64),visibility:'unknown',quality_flags:[{code:'visibility_unverified',detail:'Audience not established'}],observed_at:draft.created_at,source_session_id:'source1',quote:null};}finally{activeReads--;}});
 render(<HistoryView worker={w}/>);await screen.findByLabelText('Select revision rev-8');for(const id of ids)fireEvent.click(screen.getByLabelText(`Select revision ${id}`));fireEvent.click(screen.getByRole('button',{name:'Prepare send from selected posts'}));
 await waitFor(()=>expect(completed).toEqual(ids));expect(peakReads).toBeLessThanOrEqual(3);expect(w.queryArchiveRevision).toHaveBeenCalledTimes(12);fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));await waitFor(()=>expect(w.configureDeliveryDraft).toHaveBeenCalledWith(expect.objectContaining({revision_ids:ids,source_policy:'captured'})));expect(w.approveDeliveryDraft).not.toHaveBeenCalled();
});

it('takes an approved plan directly to Deliveries and closes the composer without sending',async()=>{
 const w=await compose();fireEvent.change(screen.getByLabelText('Saved destination'),{target:{value:'0'}});fireEvent.click(screen.getByRole('button',{name:'Save draft and preview'}));
 await screen.findByRole('region',{name:'Exact message preview'});fireEvent.click(screen.getByRole('button',{name:'Save message plan'}));
 await waitFor(()=>expect(screen.getByRole('tab',{name:'Deliveries'})).toHaveAttribute('aria-selected','true'));
 expect(screen.queryByRole('region',{name:'Prepare send'})).not.toBeInTheDocument();expect(screen.getByRole('region',{name:'Delivery jobs'})).toBeVisible();expect(w.approveDeliveryDraft).toHaveBeenCalledTimes(1);
});

it('keeps an in-progress send visible and refreshes recorded outcomes on completion',async()=>{
 const manualTarget={...target,destination_id:'-456'},current={...job,target:manualTarget};
 let finish!:(value:unknown)=>void;
 const w={...worker(),queryAccount:vi.fn().mockResolvedValue({snapshot:{account_id:'123',epoch:2,auth_state:'ready',network_state:'online',revocation_pending:false}}),
  previewManualHistorySend:vi.fn().mockResolvedValue({job_id:current.id,job_version:current.version,revision_id:current.revision_id,text:current.text,target:manualTarget,destination:{...preview.destination,chat_id:'-456'},preview_receipt:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString()}),
  sendManualHistoryOnce:vi.fn().mockResolvedValue(undefined),queryManualHistorySend:vi.fn().mockImplementation(()=>new Promise(resolve=>{finish=resolve;}))};
 w.queryDeliveryJobs.mockResolvedValue({items:[current],next_cursor:null});
 w.queryHistory.mockResolvedValueOnce([]).mockResolvedValue([{delivery:{id:'delivery1',post_id:'post1',destination_id:'-456',sender:target.sender,state:'sent',route_refs:[]},attempts:[]}]);
 render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));
 const send=await screen.findByRole('button',{name:'Send now'});await waitFor(()=>expect(send).toBeEnabled());fireEvent.click(send);
 fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:'Send this message'}));await waitFor(()=>expect(w.queryManualHistorySend).toHaveBeenCalled());
 expect(screen.getByRole('tab',{name:'Posts'})).toBeDisabled();expect(screen.getByRole('tab',{name:'Model runs'})).toBeDisabled();
 await act(async()=>finish({state:'sent',job_id:current.id,job_version:current.version,reason:null}));
 expect(within(await screen.findByRole('table',{name:'Delivery outcomes'})).getByText('Sent')).toBeVisible();expect(screen.getByRole('tab',{name:'Posts'})).toBeEnabled();expect(w.sendManualHistoryOnce).toHaveBeenCalledTimes(1);
});

it('finishes the open draft before allowing another job to start sending',async()=>{
 const w={...worker(),queryAccount:vi.fn().mockResolvedValue({snapshot:{account_id:'123',epoch:2,auth_state:'ready',network_state:'online',revocation_pending:false}}),previewManualHistorySend:vi.fn(),sendManualHistoryOnce:vi.fn(),queryManualHistorySend:vi.fn()};
 w.queryDeliveryJobs.mockResolvedValue({items:[{...job,target:{...target,destination_id:'-456'}}],next_cursor:null});
 await compose(w);fireEvent.click(screen.getByRole('tab',{name:'Deliveries'}));
 const send=await screen.findByRole('button',{name:'Send now'});await waitFor(()=>expect(w.queryAccount).toHaveBeenCalled());
 expect(send).toBeDisabled();expect(screen.getByText('Finish or close the open draft before sending this message.')).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Close composer'}));await waitFor(()=>expect(send).toBeEnabled());
 expect(w.previewManualHistorySend).not.toHaveBeenCalled();expect(w.sendManualHistoryOnce).not.toHaveBeenCalled();
});
