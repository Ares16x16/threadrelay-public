import type { ActivityEntry } from '../../lib/contracts';

/** Presentation only: the underlying journal keeps every occurrence. */
export function groupEvents(entries:ActivityEntry[]) {
 const groups:{entry:ActivityEntry;occurrences:ActivityEntry[]}[]=[];
 const latest=new Map<string,typeof groups[number]>();
 for(const entry of entries){
  const {sequence:_sequence,at:_at,...identity}=entry;
  const key=JSON.stringify(identity),previous=latest.get(key);
  const gap=previous?Date.parse(previous.entry.at)-Date.parse(entry.at):Infinity;
  if(previous&&gap>=0&&gap<=60000)previous.occurrences.push(entry);
  else {const group={entry,occurrences:[entry]};groups.push(group);latest.set(key,group);}
 }
 return groups;
}
