import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { HistoryView } from './View';

const post={post_id:'p1',permalink:'https://www.threads.com/@alice/post/p1',author:'alice',text:'A permanent first observation',content_hash:'a'.repeat(64),visibility:'public' as const,quality_flags:[],observed_at:'2026-11-01T00:00:00Z',source_session_id:'source',quote:{author:'bob',text:'An attributed quote',permalink:null}};
const summary={revision_id:'rev-1',post_id:post.post_id,permalink:post.permalink,author:post.author,text_preview:post.text,observed_at:post.observed_at,visibility:post.visibility,quality_flags:[],provenance:'captured' as const};
function worker(){return {queryHistory:vi.fn().mockResolvedValue([]),queryArchivePosts:vi.fn().mockResolvedValue({items:[summary],next_cursor:'next'}),queryArchiveRevision:vi.fn().mockResolvedValue(post),queryArchiveRuns:vi.fn().mockResolvedValue({items:[],next_cursor:null}),queryArchiveRun:vi.fn(),queryAccount:vi.fn().mockRejectedValue(new Error('logged out'))};}

it('reads and pages permanent posts without account access, resets the cursor on search, and opens exact revision detail',async()=>{
 const w=worker();render(<HistoryView worker={w}/>);
 expect(await screen.findByText(post.text)).toBeVisible();expect(w.queryAccount).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'Next posts page'}));
 await waitFor(()=>expect(w.queryArchivePosts).toHaveBeenLastCalledWith({cursor:'next',limit:20,search:''}));
 fireEvent.change(screen.getByLabelText('Search archived posts'),{target:{value:'alice'}});fireEvent.click(screen.getByRole('button',{name:'Search posts'}));
 await waitFor(()=>expect(w.queryArchivePosts).toHaveBeenLastCalledWith({cursor:null,limit:20,search:'alice'}));
 fireEvent.click(screen.getByRole('button',{name:'Inspect post by alice'}));
 const detail=await screen.findByRole('region',{name:'Post details'});
 expect(within(detail).getByRole('heading',{name:'Post details'})).toHaveFocus();expect(within(detail).getByText(/An attributed quote/)).toBeVisible();expect(w.queryArchiveRevision).toHaveBeenCalledWith('rev-1');
 expect(within(detail).getByText(post.permalink)).toBeVisible();expect(within(detail).queryByRole('link')).not.toBeInTheDocument();
});

it('keeps historical failed and cancelled runs visible and shows exact saved inputs and topic versions',async()=>{
 const w=worker();const run={run_id:'run-1',kind:'sample',revision_id:null,started_at:'2026-11-01T00:00:00Z',finished_at:'2026-11-01T00:00:01Z',status:'cancelled',classification:null,error_code:'cancelled',input_preview:'Saved sample text',topic_count:1,binding:{model_version:'model-v1',prompt_version:'v2'},provenance:'captured'};
 w.queryArchiveRuns.mockResolvedValue({items:[run,{...run,run_id:'run-2',status:'failed',error_code:'model_unavailable'}],next_cursor:null});
 w.queryArchiveRun.mockResolvedValue({...run,input_snapshot:{text:'Exact permanently saved input'},topics:[{id:'dating',version:3,description:'Relationships as previously defined',positive_examples:['example'],negative_examples:[],exclusions:[],enabled:true}]});
 render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Model runs'}));
 expect(await screen.findByText('Cancelled')).toBeVisible();expect(screen.getByText('Failed')).toBeVisible();
 fireEvent.click(screen.getByRole('button',{name:'Inspect run run-1'}));
 const detail=await screen.findByRole('region',{name:'Model run details'});
 expect(within(detail).getByText('Exact permanently saved input')).toBeVisible();expect(within(detail).getByText(/Relationships as previously defined/, {selector:'summary'})).toBeVisible();expect(within(detail).getByText(/version 3/)).toBeVisible();
 expect(within(detail).queryByRole('button',{name:/prepare send/i})).not.toBeInTheDocument();expect(w.queryAccount).not.toHaveBeenCalled();
});

it('does not display an earlier post detail after a newer revision is selected',async()=>{
 const w=worker();w.queryArchivePosts.mockResolvedValue({items:[summary,{...summary,revision_id:'rev-2',author:'carol',text_preview:'Second revision'}],next_cursor:null});
 let old!:(p:typeof post)=>void;w.queryArchiveRevision.mockImplementation((id:string)=>id==='rev-1'?new Promise(r=>{old=r;}):Promise.resolve({...post,author:'carol',text:'Current detail'}));
 render(<HistoryView worker={w}/>);fireEvent.click(await screen.findByRole('button',{name:'Inspect post by alice'}));fireEvent.click(screen.getByRole('button',{name:'Inspect post by carol'}));
 await screen.findByText('Current detail');await act(async()=>old({...post,text:'Stale detail'}));expect(screen.queryByText('Stale detail')).not.toBeInTheDocument();
});

it('supports arrow-key navigation between History tabs',async()=>{
 const w=worker();render(<HistoryView worker={w}/>);const posts=screen.getByRole('tab',{name:'Posts'});posts.focus();fireEvent.keyDown(posts,{key:'ArrowRight'});
 expect(screen.getByRole('tab',{name:'Model runs'})).toHaveAttribute('aria-selected','true');expect(screen.getByRole('tab',{name:'Model runs'})).toHaveFocus();await waitFor(()=>expect(w.queryArchiveRuns).toHaveBeenCalled());
});

it.each([undefined,0.95])('labels the exact saved topic version and threshold beside historical results (topic minimum %s)',async(minimum)=>{
 const w=worker();
 const topic={id:'romance',version:4,description:'Earlier broad relationship definition',positive_examples:[],negative_examples:[],exclusions:[],enabled:true,...(minimum===undefined?{}:{minimum_score:minimum})};
 const run={run_id:'historical',kind:'sample',revision_id:null,started_at:'2026-11-01T00:00:00Z',finished_at:'2026-11-01T00:00:01Z',status:'succeeded',classification:{post_id:'saved',model_version:'pinned-model',topic_results:[{topic_id:'romance',match:true,abstained:false,score:0.96}],abstained:false},error_code:null,input_preview:'A saved historical input',topic_count:1,binding:{positive_threshold:0.9},provenance:'captured'};
 w.queryArchiveRuns.mockResolvedValue({items:[run],next_cursor:null});
 w.queryArchiveRun.mockResolvedValue({...run,input_snapshot:{text:'A saved historical input'},topics:[topic]});
 render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Model runs'}));
 fireEvent.click(await screen.findByRole('button',{name:'Inspect run historical'}));
 const detail=await screen.findByRole('region',{name:'Model run details'});
 expect(await within(detail).findByText(new RegExp(`romance: Match.*saved topic v4.*minimum score ${minimum??0.9}`))).toBeVisible();
 expect(within(detail).getByText('Editing Topics does not reclassify saved runs. Each result uses its saved definition and threshold.')).toBeVisible();
 expect(w.queryAccount).not.toHaveBeenCalled();
});


it('shows saved classification scores in the model run list without opening technical details',async()=>{
 const w=worker();
 const run={run_id:'scored',kind:'collection',revision_id:'rev-1',started_at:null,finished_at:null,status:'succeeded',classification:{post_id:'p1',model_version:'saved-model',topic_results:[{topic_id:'crush',match:true,abstained:false,score:0.96},{topic_id:'other',match:false,abstained:false,score:0},{topic_id:'uncertain',match:false,abstained:true,score:null}],abstained:true},error_code:null,input_preview:'Scored post',topic_count:3,binding:{},provenance:'captured'};
 w.queryArchiveRuns.mockResolvedValue({items:[run],next_cursor:null});
 render(<HistoryView worker={w}/>);fireEvent.click(screen.getByRole('tab',{name:'Model runs'}));
 expect(await screen.findByText(/crush: Match · Model score 96%/)).toBeVisible();
 expect(screen.getByText(/other: No match · Model score 0%/)).toBeVisible();
 expect(screen.getByText(/uncertain: Abstained · Score unavailable/)).toBeVisible();
 expect(w.queryArchiveRun).not.toHaveBeenCalled();
});

it('shows the latest saved score and threshold when inspecting a post',async()=>{
 const w=worker();
 const run={run_id:'latest',kind:'collection',revision_id:'rev-1',started_at:null,finished_at:null,status:'succeeded',classification:{post_id:'p1',model_version:'saved-model',topic_results:[{topic_id:'crush',match:true,abstained:false,score:0.965}],abstained:false},error_code:null,input_preview:'Scored post',topic_count:1,binding:{positive_threshold:0.9},provenance:'captured'};
 w.queryArchiveRuns.mockResolvedValue({items:[run],next_cursor:null});
 w.queryArchiveRun.mockResolvedValue({...run,input_snapshot:{post},topics:[{id:'crush',version:2,minimum_score:0.95}]});
 render(<HistoryView worker={w}/>);fireEvent.click(await screen.findByRole('button',{name:'Inspect post by alice'}));
 const detail=await screen.findByRole('region',{name:'Post details'});
 expect(await within(detail).findByText(/Model score 96.5%/)).toBeVisible();
 expect(within(detail).getByText(/minimum score 0.95/)).toBeVisible();
 expect(w.queryArchiveRuns).toHaveBeenCalledWith({revision_id:'rev-1',limit:1});
 expect(w.queryAccount).not.toHaveBeenCalled();
});
