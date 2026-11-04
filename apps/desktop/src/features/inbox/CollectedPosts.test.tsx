import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { CollectionItem, CollectionList } from '../../lib/contracts';
import { CollectedPosts } from './CollectedPosts';

const post:CollectionItem['post']={post_id:'one',permalink:'https://www.threads.net/@a/post/one',author:'a',text:'Collected source',content_hash:'b'.repeat(64),visibility:'public',quality_flags:[],observed_at:'2026-01-01T00:00:00Z',source_session_id:'s',quote:null};
function row(id:string,topic:string,match:boolean,abstained=false):CollectionItem {
  return {post:{...post,post_id:id,text:id},topics:[{id:topic,label:topic}],classification:{post_id:id,model_version:'fixture',topic_results:[{topic_id:topic,match,abstained}],abstained}};
}
afterEach(()=>vi.useRealTimers());

it('automatically shows a new current match after an initially empty collection',async()=>{
  vi.useFakeTimers();
  let rows:CollectionList=[];
  const worker={queryCollection:vi.fn(async()=>rows)};
  await act(async()=>{render(<CollectedPosts worker={worker}/>);});
  expect(screen.getByText(/No collected posts yet/)).toBeVisible();
  rows=[row('New match','Science',true)];
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByText('New match')).toBeVisible();
  expect(screen.getByLabelText('Result filter')).toHaveValue('match');
});

it('keeps the selected topic and result while new results arrive',async()=>{
  vi.useFakeTimers();
  let rows=[row('Science match','Science',true),row('Relationship uncertain','Relationships',false,true)];
  await act(async()=>{render(<CollectedPosts worker={{queryCollection:async()=>rows}}/>);});
  fireEvent.change(screen.getByLabelText('Topic filter'),{target:{value:'Relationships'}});
  fireEvent.change(screen.getByLabelText('Result filter'),{target:{value:'abstained'}});
  rows=[...rows,row('Another uncertain result','Relationships',false,true)];
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByLabelText('Topic filter')).toHaveValue('Relationships');
  expect(screen.getByLabelText('Result filter')).toHaveValue('abstained');
  expect(screen.getByText('Another uncertain result')).toBeVisible();
  expect(screen.queryByText('Science match')).not.toBeInTheDocument();
});

it('explains a zero-match collection and shows all results in one click',async()=>{
  const rows=[row('Uncertain source','Science',false,true),row('Unrelated source','Science',false),{post:{...post,post_id:'changed',text:'Changed source'},classification:null,topics:[]}];
  render(<CollectedPosts worker={{queryCollection:async()=>rows}}/>);
  expect(await screen.findByText(/Matches 0 · Abstained 1 · No match 1 · Needs classification 1/)).toBeVisible();
  expect(screen.getByText(/Matches use your current topics/)).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'Show all results'}));
  expect(screen.getByText('Uncertain source')).toBeVisible();
  expect(screen.getByText('Unrelated source')).toBeVisible();
  expect(screen.getByText('Changed source')).toBeVisible();
  expect(screen.getByLabelText('Result filter')).toHaveValue('all');
});

it('shows any current topic match and keeps a specific topic when showing all its results',async()=>{
  const multi=row('Multiple topics','Science',false,true);
  multi.topics.push({id:'Relationships',label:'Relationships'});
  multi.classification!.topic_results.push({topic_id:'Relationships',match:true,abstained:false});
  render(<CollectedPosts worker={{queryCollection:async()=>[multi,row('Other science result','Science',false)]}}/>);
  expect(await screen.findByText('Multiple topics')).toBeVisible();
  fireEvent.change(screen.getByLabelText('Topic filter'),{target:{value:'Science'}});
  expect(screen.queryByText('Multiple topics')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Show all results'}));
  expect(screen.getByLabelText('Topic filter')).toHaveValue('Science');
  expect(screen.getByText('Multiple topics')).toBeVisible();
  expect(screen.getByText('Other science result')).toBeVisible();
});

it('retains rows during a pending read and labels them stale after a failed refresh',async()=>{
  vi.useFakeTimers();
  let rejectRead:(reason:Error)=>void=()=>{};
  let connected=true;
  const worker={queryCollection:vi.fn(()=>connected?Promise.resolve([row('Last loaded match','Science',true)]):new Promise<CollectionList>((_,reject)=>{rejectRead=reject;}))};
  await act(async()=>{render(<CollectedPosts worker={worker}/>);});
  connected=false;
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.getByText('Last loaded match')).toBeVisible();
  await act(async()=>{rejectRead(new Error('offline'));});
  expect(screen.getByRole('alert')).toHaveTextContent('Last known data shown');
  expect(screen.getByText(/Last loaded results: Matches 1/)).toBeVisible();
  const reads=worker.queryCollection.mock.calls.length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(15000);});
  expect(worker.queryCollection).toHaveBeenCalledTimes(reads);
  connected=true;
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'Refresh collected posts'}));});
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByText(/Last loaded results:/)).not.toBeInTheDocument();
});

it('removes a superseded match on an automatic refresh without reviving old labels',async()=>{
  vi.useFakeTimers();
  let rows:CollectionList=[row('Changed source','Science',true)];
  await act(async()=>{render(<CollectedPosts worker={{queryCollection:async()=>rows}}/>);});
  rows=[{...rows[0],classification:null,topics:[]}];
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(screen.queryByText('Science: Match')).not.toBeInTheDocument();
  expect(screen.queryByText('Changed source')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Show all results'}));
  expect(screen.getByText('Changed source')).toBeVisible();
  expect(screen.getByText(/Classification unavailable or changed/)).toBeVisible();
});

it('reclassifies an exact saved post with current topics and refreshes without sending',async()=>{
 let rows:CollectionList=[{post,classification:null,topics:[]}];
 const worker={queryCollection:vi.fn(async()=>rows),testSample:vi.fn(async()=>{rows=[{...row(post.post_id,'Romantic feelings',true),post}];return {classification:rows[0].classification!,sending_possible:false as const};})};
 render(<CollectedPosts worker={worker}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Show all results'}));
 fireEvent.click(screen.getByRole('button',{name:'Reclassify with current topics'}));
 expect(await screen.findByText('Romantic feelings: Match')).toBeVisible();
 expect(worker.testSample).toHaveBeenCalledExactlyOnceWith(post.text,post.post_id);
 fireEvent.change(screen.getByLabelText('Result filter'),{target:{value:'match'}});expect(screen.getByText(post.text)).toBeVisible();
});
