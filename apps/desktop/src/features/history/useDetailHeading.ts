import { useEffect, useRef } from 'react';
/** Bring an explicitly opened detail into view without focusing query refreshes. */
export function useDetailHeading() {
 const heading=useRef<HTMLHeadingElement>(null);
 useEffect(()=>{heading.current?.focus();heading.current?.scrollIntoView?.({block:'start'});},[]);
 return heading;
}
