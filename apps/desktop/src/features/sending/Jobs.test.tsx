import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { DeliveryJob } from '../../lib/contracts';
import { DeliveryJobsView } from './Jobs';

export const manualJob:DeliveryJob={id:'job1',version:3,kind:'job',draft_id:'draft1',draft_version:1,revision_id:'revision1',post_id:'post1',text:'One saved message',target:{account_id:'123',account_epoch:2,destination_id:'-456',destination_version:4,sender:{kind:'user',id:'123'}},due_at:null,timing:{mode:'now',timezone:'Asia/Hong_Kong'},status:'blocked',blocked_reasons:['native_delivery_unavailable'],requires_approval:false,rule_id:null,delivery_id:null,attempt_id:null,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z',source_policy:'public_only'};
export function manualWorker(){return {
 queryAccount:vi.fn().mockResolvedValue({snapshot:{account_id:'123',epoch:2,auth_state:'ready',network_state:'online',revocation_pending:false}}),
 queryDeliveryWorkspace:vi.fn().mockResolvedValue({templates:[],targets:[],drafts:[],setup_reasons:[]}),saveMessageTemplate:vi.fn(),configureDeliveryDraft:vi.fn(),getDeliveryDraft:vi.fn(),previewDeliveryDraft:vi.fn(),approveDeliveryDraft:vi.fn(),queryDeliveryJobs:vi.fn().mockResolvedValue({items:[manualJob],next_cursor:null}),pauseDeliveryJob:vi.fn(),resumeDeliveryJob:vi.fn(),cancelDeliveryJob:vi.fn(),
 previewManualHistorySend:vi.fn().mockResolvedValue({job_id:manualJob.id,job_version:manualJob.version,revision_id:manualJob.revision_id,text:manualJob.text,destination:{account_id:'123',chat_id:'-456',title:'My saved group',audience:'public',sender:{kind:'user',id:'123'},permissions:{can_send:true,can_select_sender:false},paid_required:false},target:manualJob.target,preview_receipt:'a'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString()}),
 sendManualHistoryOnce:vi.fn().mockResolvedValue(undefined),queryManualHistorySend:vi.fn().mockResolvedValue({state:'sent',job_id:manualJob.id,job_version:manualJob.version,reason:null}),
};}

async function chooseSendNow(){const button=await screen.findByRole('button',{name:'Send now'});await waitFor(()=>expect(button).toBeEnabled());fireEvent.click(button);}

it('offers Send now only for standalone approved immediate jobs',async()=>{
 const worker=manualWorker();worker.queryDeliveryJobs.mockResolvedValue({items:[manualJob,{...manualJob,id:'rule',kind:'rule'},{...manualJob,id:'rule-child',rule_id:'rule'},{...manualJob,id:'approval',requires_approval:true},{...manualJob,id:'unknown',status:'unknown'},{...manualJob,id:'scheduled',timing:{mode:'at_time',at:'2026-11-02T00:00:00Z',timezone:'Asia/Hong_Kong'}}] as DeliveryJob[],next_cursor:null});
 render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 expect(await screen.findAllByRole('button',{name:'Send now'})).toHaveLength(1);expect(worker.previewManualHistorySend).not.toHaveBeenCalled();expect(worker.sendManualHistoryOnce).not.toHaveBeenCalled();
});
it('requires explicit Resume for paused jobs and offers no manual send for known unsupported targets',async()=>{
 const worker=manualWorker();worker.queryDeliveryJobs.mockResolvedValue({items:[manualJob,{...manualJob,id:'paused',status:'paused'},{...manualJob,id:'private',target:{...manualJob.target!,destination_id:'456'}},{...manualJob,id:'chat-sender',target:{...manualJob.target!,sender:{kind:'chat',id:'-999'}}},{...manualJob,id:'foreign-sender',target:{...manualJob.target!,sender:{kind:'user',id:'999'}}}],next_cursor:null});
 render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);expect(await screen.findAllByRole('button',{name:'Send now'})).toHaveLength(1);
 const paused=screen.getByRole('article',{name:'Delivery job paused'});expect(within(paused).queryByRole('button',{name:'Send now'})).not.toBeInTheDocument();expect(worker.resumeDeliveryJob).not.toHaveBeenCalled();fireEvent.click(within(paused).getByRole('button',{name:'Resume job'}));await waitFor(()=>expect(worker.resumeDeliveryJob).toHaveBeenCalledExactlyOnceWith({job_id:'paused',version:3}));
});
it('previews the exact recipient and message before sending once, then refreshes jobs on confirmed completion',async()=>{
 const worker=manualWorker();render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 await chooseSendNow();
 const dialog=await screen.findByRole('dialog',{name:'Send this message?'});expect(dialog).toHaveTextContent('My saved group · -456');expect(dialog).toHaveTextContent('One saved message');expect(dialog).toHaveTextContent('Sender user · 123');expect(dialog).toHaveTextContent('One message. Your Telegram login stays connected.');
 expect(worker.sendManualHistoryOnce).not.toHaveBeenCalled();fireEvent.click(within(dialog).getByRole('button',{name:'Send this message'}));
 await waitFor(()=>expect(worker.sendManualHistoryOnce).toHaveBeenCalledExactlyOnceWith({job_id:'job1',version:3,preview_receipt:'a'.repeat(64)}));await waitFor(()=>expect(worker.queryDeliveryJobs).toHaveBeenCalledTimes(2));
});
it('retains an unknown outcome and prevents resend even if a refresh still returns the earlier job row',async()=>{
 const worker=manualWorker();let finish!:(value:{items:DeliveryJob[];next_cursor:null})=>void;worker.queryDeliveryJobs.mockResolvedValueOnce({items:[manualJob],next_cursor:null}).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));worker.queryManualHistorySend.mockResolvedValue({state:'unknown',job_id:manualJob.id,job_version:manualJob.version,reason:'dispatch_unconfirmed'} as never);render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);await chooseSendNow();fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:'Send this message'}));
 await waitFor(()=>expect(worker.queryDeliveryJobs).toHaveBeenCalledTimes(2));await act(async()=>finish({items:[manualJob],next_cursor:null}));expect(await screen.findByText(/Outcome unknown\. Do not resend/)).toBeVisible();expect(screen.queryByRole('button',{name:'Send now'})).not.toBeInTheDocument();
});
it.each([['unknown','Outcome unknown. Do not resend.'],['failed','Send failed.'],['sent','Message sent.']] as const)('keeps the %s outcome visible when the terminal jobs refresh fails',async(result,expected)=>{
 const worker=manualWorker();worker.queryDeliveryJobs.mockResolvedValueOnce({items:[manualJob],next_cursor:null}).mockRejectedValueOnce(new Error('unavailable'));worker.queryManualHistorySend.mockResolvedValue({state:result,job_id:manualJob.id,job_version:manualJob.version,reason:null});
 render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);await chooseSendNow();fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:'Send this message'}));
 await screen.findByText('Could not load current data. Retry when the worker is available.');expect(screen.getByText((value)=>value.startsWith(expected))).toBeVisible();expect(screen.queryByRole('button',{name:'Send now'})).not.toBeInTheDocument();expect(worker.sendManualHistoryOnce).toHaveBeenCalledTimes(1);
});
it('keeps the message, named destination and manual action visible with secondary controls in Details',async()=>{
 const worker=manualWorker();worker.queryDeliveryWorkspace.mockResolvedValue({templates:[],drafts:[],setup_reasons:[],targets:[{target:manualJob.target,destination:(await worker.previewManualHistorySend()).destination}]} as never);render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 const row=await screen.findByRole('article',{name:'Delivery job job1'});expect(within(row).getByText('One saved message')).toBeVisible();expect(await within(row).findByText('To My saved group')).toBeVisible();expect(within(row).getByRole('button',{name:'Send now'})).toBeVisible();expect(within(row).getByText('Details').closest('details')).not.toHaveAttribute('open');expect(within(row).getByRole('button',{name:'Cancel job'})).not.toBeVisible();expect(within(row).getByText('job1')).not.toBeVisible();
});
it('replaces background-only verification blockers with manual review while retaining other actual blockers',async()=>{
 const worker=manualWorker();worker.queryDeliveryJobs.mockResolvedValue({items:[{...manualJob,blocked_reasons:['native_delivery_unavailable','destination_unverified']},{...manualJob,id:'offline',blocked_reasons:['native_delivery_unavailable','destination_unverified','transport_offline']}],next_cursor:null});render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 const ready=await screen.findByRole('article',{name:'Delivery job job1'});expect(within(ready).getByText('Ready for manual review.')).toBeVisible();expect(within(ready).queryByText(/Verify this destination in Routes/)).not.toBeInTheDocument();
 const offline=screen.getByRole('article',{name:'Delivery job offline'});expect(within(offline).getByText(/Telegram is offline/)).toBeVisible();expect(within(offline).queryByText('Ready for manual review.')).not.toBeInTheDocument();
});

it('requires the exact connected account before showing a job as ready or preparing it',async()=>{
 const worker={...manualWorker(),queryAccount:vi.fn().mockResolvedValue({snapshot:{account_id:'123',epoch:2,auth_state:'unknown',network_state:'offline',revocation_pending:false}})};
 const openAccounts=vi.fn();render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()} onOpenAccounts={openAccounts}/>);
 const send=await screen.findByRole('button',{name:'Send now'});await waitFor(()=>expect(send).toBeDisabled());
 expect(screen.queryByText('Ready for manual review.')).not.toBeInTheDocument();
 expect(screen.getByText('Connect Telegram to send this message.')).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Open Accounts'}));expect(openAccounts).toHaveBeenCalledOnce();
 expect(worker.previewManualHistorySend).not.toHaveBeenCalled();expect(worker.sendManualHistoryOnce).not.toHaveBeenCalled();
});
it('keeps sending locked when the current account differs from the approved target',async()=>{
 const worker={...manualWorker(),queryAccount:vi.fn().mockResolvedValue({snapshot:{account_id:'999',epoch:2,auth_state:'ready',network_state:'online',revocation_pending:false}})};
 render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 const send=await screen.findByRole('button',{name:'Send now'});await waitFor(()=>expect(send).toBeDisabled());
 expect(screen.queryByText('Ready for manual review.')).not.toBeInTheDocument();expect(screen.getByText(/The account changed/)).toBeVisible();
});

it('shows a newly approved plan from the first page of all statuses',async()=>{
 const worker=manualWorker();worker.queryDeliveryJobs.mockResolvedValue({items:[manualJob],next_cursor:'20'} as never);
 const ui=render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 fireEvent.change(await screen.findByLabelText('Job status'),{target:{value:'sent'}});
 await waitFor(()=>expect(worker.queryDeliveryJobs).toHaveBeenLastCalledWith({cursor:null,limit:20,status:'sent'}));
 fireEvent.click(await screen.findByRole('button',{name:'Next jobs page'}));await waitFor(()=>expect(worker.queryDeliveryJobs).toHaveBeenLastCalledWith({cursor:'20',limit:20,status:'sent'}));
 ui.rerender(<DeliveryJobsView worker={worker} revision={1} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 await waitFor(()=>expect(screen.getByLabelText('Job status')).toHaveValue(''));
 await waitFor(()=>expect(worker.queryDeliveryJobs).toHaveBeenLastCalledWith({cursor:null,limit:20,status:null}));
});

it('keeps navigation busy while reviewing a manual send and releases it on cancellation',async()=>{
 const worker=manualWorker(),onBusy=vi.fn();render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()} onBusy={onBusy}/>);
 const send=await screen.findByRole('button',{name:'Send now'});await waitFor(()=>expect(send).toBeEnabled());fireEvent.click(send);
 const dialog=await screen.findByRole('dialog');expect(onBusy).toHaveBeenLastCalledWith(true);fireEvent.click(within(dialog).getByRole('button',{name:'Cancel'}));
 await waitFor(()=>expect(onBusy).toHaveBeenLastCalledWith(false));expect(worker.sendManualHistoryOnce).not.toHaveBeenCalled();
});

it('sends distinct jobs with separate confirmations using the same connected worker',async()=>{
 const worker=manualWorker(),second={...manualJob,id:'job2',text:'Another saved message'};
 worker.queryDeliveryJobs.mockResolvedValue({items:[manualJob,second],next_cursor:null});
 const originalPreview=await worker.previewManualHistorySend();
 worker.previewManualHistorySend.mockImplementation(async({job_id})=>({...originalPreview,job_id,text:job_id==='job2'?second.text:manualJob.text}));
 worker.queryManualHistorySend.mockResolvedValueOnce({state:'sent',job_id:'job1',job_version:3,reason:null}).mockResolvedValueOnce({state:'sent',job_id:'job2',job_version:3,reason:null});
 render(<DeliveryJobsView worker={worker} revision={0} onEdit={vi.fn()} onNewRule={vi.fn()}/>);
 for(const id of ['job1','job2']){
  const row=await screen.findByRole('article',{name:`Delivery job ${id}`});const button=within(row).getByRole('button',{name:'Send now'});await waitFor(()=>expect(button).toBeEnabled());fireEvent.click(button);
  const dialog=await screen.findByRole('dialog',{name:'Send this message?'});expect(dialog).toHaveTextContent(id==='job1'?manualJob.text!:second.text!);fireEvent.click(within(dialog).getByRole('button',{name:'Send this message'}));
  await waitFor(()=>expect(worker.sendManualHistoryOnce).toHaveBeenCalledWith({job_id:id,version:3,preview_receipt:'a'.repeat(64)}));await waitFor(()=>expect(within(row).getByText('Message sent.')).toBeVisible());
 }
 expect(worker.sendManualHistoryOnce).toHaveBeenCalledTimes(2);expect(worker.queryAccount).toHaveBeenCalled();
});
