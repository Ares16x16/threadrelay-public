import {expect,it} from 'vitest';
import {entry} from './testFixtures';
import {groupEvents} from './groupEvents';
it('groups repeated observations while preserving each occurrence',()=>{
 const first={...entry('3'),text_preview:'Same post'};
 const rows=[first,{...first,sequence:'2'},{...first,sequence:'1'}];
 const groups=groupEvents(rows);
 expect(groups).toHaveLength(1);expect(groups[0].occurrences).toEqual(rows);
});
it('keeps different operations and later recurrences separate',()=>{
 const first={...entry('3'),text_preview:'Same post',run_id:'one'};
 expect(groupEvents([first,{...first,sequence:'2',run_id:'two'}])).toHaveLength(2);
 expect(groupEvents([first,{...first,sequence:'1',at:'2026-11-01T03:58:00Z'}])).toHaveLength(2);
});
