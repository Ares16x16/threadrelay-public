import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import type { AccountSnapshot, ReviewItem } from '../../lib/contracts';
import { InboxView, ReviewDetail } from './View';

const account:AccountSnapshot = {account_id:'123',epoch:1,auth_state:'ready',display_name:'Example',network_state:'online',revocation_pending:false};
const item:ReviewItem = {topics:[{id:'science',version:1,description:'Synthetic research'}],outgoing_text:'https://www.threads.net/@a/post/Abc123',classification:{post_id:'Abc123',model_version:'fixture',topic_results:[{topic_id:'science',match:true,abstained:false}],abstained:false},proposal:{plan_id:'a'.repeat(64),route_refs:[{route_id:'r',route_version:1,approval_id:null}],destination_version:1,approval_required:true,labels:['science'],route:{id:'r',version:1,topic_ids:['science'],destination_id:'-456',mode:'review',enabled:true,account_id:'123',account_epoch:1,sender:{kind:'user',id:'123'}},destination:{account_id:'123',chat_id:'-456',title:'Target',audience:'public',permissions:{can_send:true,can_select_sender:false},sender:{kind:'user',id:'123'},paid_required:false},post:{post_id:'Abc123',permalink:'https://www.threads.net/@a/post/Abc123',author:'a',text:'Private fixture prose',content_hash:'b'.repeat(64),visibility:'public',quality_flags:[],observed_at:'2026-01-01T00:00:00Z',source_session_id:'s',quote:null}}};

it('shows actual bound identity and outgoing link and revokes an open preview after account changes', () => {
  const approve=vi.fn();
  const {rerender}=render(<ReviewDetail item={item} account={account} authoritative approve={approve} dismiss={()=>{}} />);
  expect(screen.getByText(/Example · 123/)).toBeVisible();
  expect(screen.getByText(/Public audience/)).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'Review approval'}));
  expect(screen.getByText(/Approval is staged/)).toBeVisible();
  rerender(<ReviewDetail item={item} account={{...account,epoch:2}} authoritative approve={approve} dismiss={()=>{}} />);
  expect(screen.getByText(/Details changed/)).toBeVisible();
  expect(screen.getByRole('button',{name:'Approve for session'})).toBeDisabled();
  expect(approve).not.toHaveBeenCalled();
});

it('refuses paid destinations even with a valid account', () => {
  const paid={...item,proposal:{...item.proposal,destination:{...item.proposal.destination!,paid_required:true}}};
  render(<ReviewDetail item={paid} account={account} authoritative approve={()=>{}} dismiss={()=>{}} />);
  expect(screen.getByText(/paid messaging is not supported/i)).toBeVisible();
  expect(screen.getByRole('button',{name:'Review approval'})).toBeDisabled();
});
it('freezes the reviewed destination and disables approval if route data changes', () => {
  const {rerender}=render(<ReviewDetail item={item} account={account} authoritative approve={()=>{}} dismiss={()=>{}} />);
  fireEvent.click(screen.getByRole('button',{name:'Review approval'}));
  const changed={...item,proposal:{...item.proposal,route:{...item.proposal.route,version:2},destination:{...item.proposal.destination!,title:'Changed'}}};
  rerender(<ReviewDetail item={changed} account={account} authoritative approve={()=>{}} dismiss={()=>{}} />);
  const dialog=screen.getByRole('dialog',{name:'Confirm review'});
  expect(dialog).toHaveTextContent('Target');
  expect(dialog).not.toHaveTextContent('Changed');
  expect(screen.getByRole('button',{name:'Approve for session'})).toBeDisabled();
});
it('observes backend revocation while preserving the frozen open preview', async () => {
  vi.useFakeTimers();
  try {
    let reviews=[item];let currentAccount={snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]};
    const worker={queryReview:vi.fn(async()=>reviews),queryAccount:vi.fn(async()=>currentAccount),approveReview:vi.fn(),dismissReview:vi.fn()};
    await act(async()=>{render(<InboxView worker={worker} navigate={()=>{}}/>);});
    fireEvent.click(screen.getByRole('button',{name:/a Private fixture prose/}));
    await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Review approval'}));});
    const dialog=screen.getByRole('dialog',{name:'Confirm review'});expect(dialog).toHaveTextContent('Target');
    reviews=[];currentAccount={...currentAccount,snapshot:{...account,epoch:2}};
    await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
    expect(screen.getByRole('dialog',{name:'Confirm review'})).toHaveTextContent('Target');
    expect(screen.getByText(/Details changed/)).toBeVisible();
    expect(screen.getByRole('button',{name:'Approve for session'})).toBeDisabled();
    expect(worker.approveReview).not.toHaveBeenCalled();
  } finally {vi.useRealTimers();}
});
it('revalidates against backend changes before opening a confirmation', async () => {
 let reviews=[item];let latest=account;
 const worker={queryReview:vi.fn(async()=>reviews),queryAccount:vi.fn(async()=>({snapshot:latest,challenge:null,status:'ready' as const,pending_revocations:[]})),approveReview:vi.fn(),dismissReview:vi.fn()};
 render(<InboxView worker={worker} navigate={()=>{}}/>);fireEvent.click(await screen.findByRole('button',{name:/a Private fixture prose/}));
 reviews=[];latest={...account,epoch:2};fireEvent.click(screen.getByRole('button',{name:'Review approval'}));
 expect(await screen.findByText(/Details changed — review again/)).toBeVisible();
 expect(screen.queryByRole('dialog',{name:'Confirm review'})).not.toBeInTheDocument();expect(worker.approveReview).not.toHaveBeenCalled();
});
it('disables an open confirmation if read-only observation disconnects', async () => {
 vi.useFakeTimers();
 try {
  let connected=true;const worker={queryReview:vi.fn(async()=>[item]),queryAccount:vi.fn(async()=>{if(!connected)throw new Error('offline');return {snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]};}),approveReview:vi.fn(),dismissReview:vi.fn()};
  await act(async()=>{render(<InboxView worker={worker} navigate={()=>{}}/>);});fireEvent.click(screen.getByRole('button',{name:/a Private fixture prose/}));
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Review approval'}));});expect(screen.getByRole('button',{name:'Approve for session'})).toBeEnabled();
  connected=false;await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByRole('dialog',{name:'Confirm review'})).toHaveTextContent('Target');expect(screen.getByRole('button',{name:'Approve for session'})).toBeDisabled();expect(worker.approveReview).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
it('stops automatic reads after disconnection and resumes only after an explicit safe check', async () => {
 vi.useFakeTimers();
 try {
  let connected=true;const worker={queryReview:vi.fn(async()=>[item]),queryAccount:vi.fn(async()=>{if(!connected)throw new Error('offline');return {snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]};}),approveReview:vi.fn(),dismissReview:vi.fn()};
  await act(async()=>{render(<InboxView worker={worker} navigate={()=>{}}/>);});fireEvent.click(screen.getByRole('button',{name:/a Private fixture prose/}));await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Review approval'}));});
  connected=false;await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});const calls=worker.queryAccount.mock.calls.length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});expect(worker.queryAccount).toHaveBeenCalledTimes(calls);
  connected=true;await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Check current details'}));});expect(screen.getByRole('button',{name:'Approve for session'})).toBeEnabled();
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});expect(worker.queryAccount.mock.calls.length).toBeGreaterThan(calls+1);
  expect(worker.approveReview).not.toHaveBeenCalled();
 } finally {vi.useRealTimers();}
});
it('does not restore an open approval if a disappeared plan later reappears', async () => {
 vi.useFakeTimers();
 try {
  let reviews=[item];const worker={queryReview:vi.fn(async()=>reviews),queryAccount:vi.fn(async()=>({snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]})),approveReview:vi.fn(),dismissReview:vi.fn()};
  await act(async()=>{render(<InboxView worker={worker} navigate={()=>{}}/>);});fireEvent.click(screen.getByRole('button',{name:/a Private fixture prose/}));await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Review approval'}));});
  reviews=[];await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});expect(screen.getByRole('button',{name:'Approve for session'})).toBeDisabled();
  reviews=[item];await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});expect(screen.getByRole('button',{name:'Approve for session'})).toBeDisabled();expect(screen.getByText(/Details changed/)).toBeVisible();
 } finally {vi.useRealTimers();}
});
it('refreshes read-only state after rejected approval without replaying the mutation', async () => {
 let reviews=[item];const worker={queryReview:vi.fn(async()=>reviews),queryAccount:vi.fn(async()=>({snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]})),approveReview:vi.fn(async()=>{reviews=[];throw new Error('stale');}),dismissReview:vi.fn()};
 render(<InboxView worker={worker} navigate={()=>{}}/>);fireEvent.click(await screen.findByRole('button',{name:/a Private fixture prose/}));fireEvent.click(screen.getByRole('button',{name:'Review approval'}));
 const confirm=await screen.findByRole('button',{name:'Approve for session'});fireEvent.click(confirm);
 expect(await screen.findByText(/Approval could not be staged/)).toBeVisible();
 expect(worker.approveReview).toHaveBeenCalledTimes(1);expect(worker.queryReview.mock.calls.length).toBeGreaterThanOrEqual(3);expect(worker.queryAccount.mock.calls.length).toBeGreaterThanOrEqual(3);
});
it('clears a disappeared review selection after the user closes its frozen preview', async () => {
 vi.useFakeTimers();
 try {
  let reviews=[item];const worker={queryReview:vi.fn(async()=>reviews),queryAccount:vi.fn(async()=>({snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]})),approveReview:vi.fn(),dismissReview:vi.fn()};
  await act(async()=>{render(<InboxView worker={worker} navigate={()=>{}}/>);});fireEvent.click(screen.getByRole('button',{name:/a Private fixture prose/}));await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Review approval'}));});
  reviews=[];await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});fireEvent.click(within(screen.getByRole('dialog',{name:'Confirm review'})).getByRole('button',{name:'Cancel'}));
  expect(screen.getByText('Nothing needs review')).toBeVisible();
 } finally {vi.useRealTimers();}
});
it('bulk review stages only separately acknowledged exact previews',async()=>{
 const other=structuredClone(item);other.proposal.plan_id='c'.repeat(64);other.proposal.post.post_id='Other';other.proposal.post.permalink='https://www.threads.net/@a/post/Other';other.outgoing_text=other.proposal.post.permalink;other.proposal.destination!.title='Second target';
 const worker={queryReview:vi.fn().mockResolvedValue([item,other]),queryAccount:vi.fn().mockResolvedValue({snapshot:account,challenge:null,status:'ready',pending_revocations:[]}),approveReview:vi.fn(),dismissReview:vi.fn(),bulkApproveReview:vi.fn().mockResolvedValue({results:[{plan_id:item.proposal.plan_id,status:'staged'}]}),bulkDismissReview:vi.fn()};
 render(<InboxView worker={worker} navigate={()=>{}}/>);await screen.findByText('Bulk review (up to 20 items)');
 const checks=screen.getAllByLabelText('Select post');fireEvent.click(checks[0]);
 fireEvent.click(screen.getByRole('button',{name:'Stage 1 selected approval'}));
 await screen.findByText(/1 approvals staged/);
 expect(worker.bulkApproveReview).toHaveBeenCalledTimes(1);const sent=worker.bulkApproveReview.mock.calls[0][0].items;
 expect(sent).toHaveLength(1);expect(sent[0].proposal.plan_id).toBe(item.proposal.plan_id);expect(sent[0].disclosure.plan_id).toBe(item.proposal.plan_id);expect(sent[0].disclosure.title).toBe('Target');expect(worker.approveReview).not.toHaveBeenCalled();
});

it('shows collected topic results without a Telegram account or delivery proposals', async () => {
  const collected={post:{...item.proposal.post,text:'Public guest result',quote:{author:'quoted.author',text:'Separately attributed words',permalink:null}},classification:item.classification!,topics:[{id:'science',label:'Science topic'}]};
  const worker={queryCollection:vi.fn(async()=>[collected]),queryReview:vi.fn(async()=>{throw new Error('approval unavailable');}),queryAccount:vi.fn(async()=>{throw new Error('no account');}),approveReview:vi.fn(),dismissReview:vi.fn()};
  render(<InboxView worker={worker} navigate={()=>{}}/>);
  const section=await screen.findByRole('region',{name:'Collected posts'});
  expect(await within(section).findByText('Public guest result')).toBeVisible();
  expect(within(section).getByText(/Quoted from quoted.author/)).toHaveTextContent('Separately attributed words');
  expect(within(section).getByText('Science topic: Match')).toBeVisible();
  expect(within(section).getByText(item.proposal.post.permalink,{exact:false})).toBeVisible();
  expect(within(section).queryByRole('link')).not.toBeInTheDocument();
  expect(within(section).queryByRole('button',{name:/approve|send|sign in/i})).not.toBeInTheDocument();
  expect(screen.getByRole('heading',{name:'Delivery approvals'})).toBeVisible();
  expect(worker.approveReview).not.toHaveBeenCalled();expect(worker.dismissReview).not.toHaveBeenCalled();
});

it('filters collection by topic and result without hiding abstentions as matches', async () => {
  const rows=[
    {post:{...item.proposal.post,post_id:'one',text:'Science match'},classification:{...item.classification!,post_id:'one'},topics:[{id:'science',label:'Science topic'}]},
    {post:{...item.proposal.post,post_id:'two',text:'Uncertain relationship'},classification:{...item.classification!,post_id:'two',topic_results:[{topic_id:'relationship',match:false,abstained:true}],abstained:true},topics:[{id:'relationship',label:'Relationships'}]},
    {post:{...item.proposal.post,post_id:'three',text:'Changed post'},classification:null,topics:[]}
  ];
  const worker={queryCollection:vi.fn(async()=>rows),queryReview:vi.fn(async()=>[]),queryAccount:vi.fn(async()=>({snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]})),approveReview:vi.fn(),dismissReview:vi.fn()};
  render(<InboxView worker={worker} navigate={()=>{}}/>);
  const section=await screen.findByRole('region',{name:'Collected posts'});
  expect(await within(section).findByText('Science match')).toBeVisible();
  expect(within(section).getByLabelText('Result filter')).toHaveValue('match');
  expect(within(section).queryByText('Uncertain relationship')).not.toBeInTheDocument();
  expect(within(section).queryByText('Changed post')).not.toBeInTheDocument();
  expect(within(section).getByText(/All captured posts and model runs remain in History/)).toBeVisible();
  fireEvent.change(within(section).getByLabelText('Result filter'),{target:{value:'all'}});
  expect(within(section).getByText('Relationships: Abstained')).toBeVisible();
  expect(within(section).getByText(/Classification unavailable or changed/)).toBeVisible();
  fireEvent.change(within(section).getByLabelText('Result filter'),{target:{value:'match'}});
  expect(within(section).getByText('Science match')).toBeVisible();
  expect(within(section).queryByText('Uncertain relationship')).not.toBeInTheDocument();
  expect(within(section).queryByText('Changed post')).not.toBeInTheDocument();
  fireEvent.change(within(section).getByLabelText('Topic filter'),{target:{value:'relationship'}});
  expect(within(section).getByText('No collected posts match these filters.')).toBeVisible();
  fireEvent.change(within(section).getByLabelText('Result filter'),{target:{value:'all'}});
  expect(within(section).getByText('Uncertain relationship')).toBeVisible();
  expect(within(section).queryByText('Science match')).not.toBeInTheDocument();
});

it('refreshes collection status without retaining a superseded positive classification', async () => {
  let rows=[{post:{...item.proposal.post,text:'Collected source'},classification:item.classification ?? null,topics:[{id:'science',label:'Science topic'}]}];
  let connected=true;
  const worker={queryCollection:vi.fn(async()=>{if(!connected)throw new Error('offline');return rows;}),queryReview:vi.fn(async()=>[]),queryAccount:vi.fn(async()=>({snapshot:account,challenge:null,status:'ready' as const,pending_revocations:[]})),approveReview:vi.fn(),dismissReview:vi.fn()};
  render(<InboxView worker={worker} navigate={()=>{}}/>);
  const section=await screen.findByRole('region',{name:'Collected posts'});
  expect(await within(section).findByText('Science topic: Match')).toBeVisible();
  fireEvent.change(within(section).getByLabelText('Topic filter'),{target:{value:'science'}});
  rows=[{...rows[0],classification:null,topics:[]}];
  fireEvent.click(within(section).getByRole('button',{name:'Refresh collected posts'}));
  expect(await within(section).findByText('No collected posts match these filters.')).toBeVisible();
  fireEvent.change(within(section).getByLabelText('Result filter'),{target:{value:'all'}});
  expect(await within(section).findByText(/Classification unavailable or changed/)).toBeVisible();
  expect(within(section).queryByText('Science topic: Match')).not.toBeInTheDocument();
  connected=false;fireEvent.click(within(section).getByRole('button',{name:'Refresh collected posts'}));
  expect(await within(section).findByRole('alert')).toHaveTextContent('Last known data shown');
  expect(within(section).getByText('Collected source')).toBeVisible();
  expect(worker.approveReview).not.toHaveBeenCalled();expect(worker.dismissReview).not.toHaveBeenCalled();
});
