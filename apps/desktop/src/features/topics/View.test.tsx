import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { TopicsView } from './View';

it('saves an explicitly edited topic and refreshes only after acknowledgement', async () => {
  const worker = {queryTopics: vi.fn().mockResolvedValue([]), saveTopic: vi.fn().mockResolvedValue(undefined)};
  render(<TopicsView worker={worker} />);
  await screen.findByText('Define what matters');
  fireEvent.click(screen.getByRole('button', {name:'Create topic'}));
  fireEvent.change(screen.getByLabelText('Topic ID'), {target:{value:'science'}});
  fireEvent.change(screen.getByLabelText('Description'), {target:{value:'Research'}});
  fireEvent.click(screen.getByRole('button', {name:'Save topic'}));
  await waitFor(()=>expect(worker.saveTopic).toHaveBeenCalledWith(expect.objectContaining({id:'science',description:'Research',version:1})));
  expect(worker.queryTopics).toHaveBeenCalledTimes(2);
});
it('does not overwrite an existing topic from the create form', async () => {
  const existing={id:'science',version:1,description:'Existing',positive_examples:[],negative_examples:[],exclusions:[],enabled:true};
  const worker={queryTopics:vi.fn().mockResolvedValue([existing]),saveTopic:vi.fn().mockResolvedValue(undefined)};
  render(<TopicsView worker={worker}/>);await screen.findByText('Existing');
  fireEvent.click(screen.getByRole('button',{name:'Create topic'}));
  fireEvent.change(screen.getByLabelText('Topic ID'),{target:{value:'science'}});
  fireEvent.change(screen.getByLabelText('Description'),{target:{value:'Replacement'}});
  fireEvent.click(screen.getByRole('button',{name:'Save topic'}));
  expect(screen.getByRole('alert')).toHaveTextContent(/already exists/);
  expect(worker.saveTopic).not.toHaveBeenCalled();
});
it('asks before discarding dirty topic edits when switching selection', async () => {
  const worker={queryTopics:vi.fn().mockResolvedValue([{id:'a',version:1,description:'First',positive_examples:[],negative_examples:[],exclusions:[],enabled:true},{id:'b',version:1,description:'Second',positive_examples:[],negative_examples:[],exclusions:[],enabled:true}]),saveTopic:vi.fn()};
  render(<TopicsView worker={worker}/>);await screen.findByText('First');
  fireEvent.click(screen.getByRole('button',{name:/a First/}));
  fireEvent.change(screen.getByLabelText('Description'),{target:{value:'Unsaved'}});
  fireEvent.click(screen.getByRole('button',{name:/b Second/}));
  const dialog=screen.getByRole('dialog',{name:'Discard topic edits?'});expect(dialog).toBeVisible();
  fireEvent.click(within(dialog).getByRole('button',{name:'Cancel'}));
  expect(screen.getByLabelText('Description')).toHaveValue('Unsaved');
});
it('freezes topic edits and selection while a save acknowledgment is pending', async () => {
  let finish!:()=>void;const worker={queryTopics:vi.fn().mockResolvedValue([]),saveTopic:vi.fn(()=>new Promise<void>(r=>{finish=r;}))};const onBusy=vi.fn();
  render(<TopicsView worker={worker} onBusy={onBusy}/>);await screen.findByText('Define what matters');fireEvent.click(screen.getByRole('button',{name:'Create topic'}));
  fireEvent.change(screen.getByLabelText('Topic ID'),{target:{value:'science'}});fireEvent.change(screen.getByLabelText('Description'),{target:{value:'Saved snapshot'}});fireEvent.click(screen.getByRole('button',{name:'Save topic'}));
  expect(screen.getByLabelText('Description')).toBeDisabled();expect(screen.getByRole('button',{name:'Create topic'})).toBeDisabled();expect(onBusy).toHaveBeenCalledWith(true);
  await act(async()=>finish());expect(onBusy).toHaveBeenLastCalledWith(false);
});
it('ignores a late topic save acknowledgment after unmount', async () => {
 let finish!:()=>void;const worker={queryTopics:vi.fn().mockResolvedValue([]),saveTopic:vi.fn(()=>new Promise<void>(r=>{finish=r;}))};const onDirty=vi.fn(),onBusy=vi.fn();
 const {unmount}=render(<TopicsView worker={worker} onDirty={onDirty} onBusy={onBusy}/>);await screen.findByText('Define what matters');fireEvent.click(screen.getByRole('button',{name:'Create topic'}));fireEvent.change(screen.getByLabelText('Topic ID'),{target:{value:'t'}});fireEvent.change(screen.getByLabelText('Description'),{target:{value:'Snapshot'}});fireEvent.click(screen.getByRole('button',{name:'Save topic'}));
 unmount();const dirtyCalls=onDirty.mock.calls.length,busyCalls=onBusy.mock.calls.length;await act(async()=>finish());
 expect(onDirty).toHaveBeenCalledTimes(dirtyCalls);expect(onBusy).toHaveBeenCalledTimes(busyCalls);expect(worker.queryTopics).toHaveBeenCalledTimes(1);
});
it('does not release the save lock when an onBusy callback is replaced during rerender', async () => {
 const {useState}=await import('react');let finish!:()=>void;const observed=vi.fn();const worker={queryTopics:vi.fn().mockResolvedValue([]),saveTopic:vi.fn(()=>new Promise<void>(r=>{finish=r;}))};
 function Parent(){const [busy,setBusy]=useState(false);return <><span>{busy?'busy':'idle'}</span><TopicsView worker={worker} onBusy={v=>{observed(v);setBusy(v);}}/></>;}
 render(<Parent/>);await screen.findByText('Define what matters');fireEvent.click(screen.getByRole('button',{name:'Create topic'}));fireEvent.change(screen.getByLabelText('Topic ID'),{target:{value:'t'}});fireEvent.change(screen.getByLabelText('Description'),{target:{value:'Snapshot'}});fireEvent.click(screen.getByRole('button',{name:'Save topic'}));
 expect(screen.getByText('busy')).toBeVisible();expect(observed).toHaveBeenLastCalledWith(true);
 await act(async()=>finish());expect(screen.getByText('idle')).toBeVisible();
});
it('runs the saved sample command without changing topics or staging delivery',async()=>{
 const worker={queryTopics:vi.fn().mockResolvedValue([]),saveTopic:vi.fn(),testSample:vi.fn().mockResolvedValue({sending_possible:false,classification:{topic_results:[{topic_id:'science',match:true,abstained:false}]}})};
 render(<TopicsView worker={worker}/>);fireEvent.change(screen.getByLabelText('Sample text'),{target:{value:'Synthetic sample'}});fireEvent.click(screen.getByRole('button',{name:'Classify sample without sending'}));
 expect(await screen.findByText('science: Match')).toBeVisible();expect(worker.testSample).toHaveBeenCalledWith('Synthetic sample');expect(worker.saveTopic).not.toHaveBeenCalled();
});
it('shows the existing default score and versions a changed topic minimum',async()=>{
 const topic={id:'crush',version:4,description:'A personal romantic crush',positive_examples:[],negative_examples:[],exclusions:[],enabled:true};
 const worker={queryTopics:vi.fn().mockResolvedValue([topic]),saveTopic:vi.fn().mockResolvedValue(undefined)};
 render(<TopicsView worker={worker}/>);await screen.findByText(topic.description);
 fireEvent.click(screen.getByRole('button',{name:/crush A personal romantic crush/}));
 expect(screen.getByLabelText('Minimum relevance score')).toHaveValue(.9);
 expect(screen.getByText(/not a probability/i)).toBeVisible();
 fireEvent.change(screen.getByLabelText('Minimum relevance score'),{target:{value:'.95'}});
 fireEvent.click(screen.getByRole('button',{name:'Save topic'}));
 await waitFor(()=>expect(worker.saveTopic).toHaveBeenCalledWith(expect.objectContaining({id:'crush',minimum_score:.95,version:5})));
});
it('rejects an empty or out of range minimum without saving',async()=>{
 const worker={queryTopics:vi.fn().mockResolvedValue([]),saveTopic:vi.fn()};
 render(<TopicsView worker={worker}/>);await screen.findByText('Define what matters');
 fireEvent.click(screen.getByRole('button',{name:'Create topic'}));
 fireEvent.change(screen.getByLabelText('Topic ID'),{target:{value:'crush'}});
 fireEvent.change(screen.getByLabelText('Description'),{target:{value:'A personal romantic crush'}});
 const minimum=screen.getByLabelText('Minimum relevance score');
 for(const value of ['', '0.1', '1.01']){
   fireEvent.change(minimum,{target:{value}});fireEvent.submit(minimum.closest('form')!);
   expect(screen.getByRole('alert')).toHaveTextContent(/0.11.*1/);
 }
 expect(worker.saveTopic).not.toHaveBeenCalled();
});
it('labels a returned sample score without claiming a probability',async()=>{
 const worker={queryTopics:vi.fn().mockResolvedValue([]),saveTopic:vi.fn(),testSample:vi.fn().mockResolvedValue({sending_possible:false,classification:{topic_results:[{topic_id:'crush',match:true,abstained:false,score:.93}]}})};
 render(<TopicsView worker={worker}/>);
 fireEvent.change(screen.getByLabelText('Sample text'),{target:{value:'Synthetic sample'}});
 fireEvent.click(screen.getByRole('button',{name:'Classify sample without sending'}));
 expect(await screen.findByText(/crush: Match.*relevance score 0.93/)).toBeVisible();
 expect(screen.getByText(/not a probability/i)).toBeVisible();
});

it('exposes the enabled setting as a labelled switch and saves its changed state', async () => {
  const worker = {queryTopics: vi.fn().mockResolvedValue([]), saveTopic: vi.fn().mockResolvedValue(undefined)};
  render(<TopicsView worker={worker}/>);
  await screen.findByText('Define what matters');
  fireEvent.click(screen.getByRole('button', {name:'Create topic'}));
  fireEvent.change(screen.getByLabelText('Topic ID'), {target:{value:'science'}});
  fireEvent.change(screen.getByLabelText('Description'), {target:{value:'Research'}});
  const enabled = screen.getByRole('switch', {name:'Enabled'});
  expect(enabled).toBeChecked();
  fireEvent.click(enabled);
  expect(enabled).not.toBeChecked();
  fireEvent.click(screen.getByRole('button', {name:'Save topic'}));
  await waitFor(()=>expect(worker.saveTopic).toHaveBeenCalledWith(expect.objectContaining({enabled:false})));
});
