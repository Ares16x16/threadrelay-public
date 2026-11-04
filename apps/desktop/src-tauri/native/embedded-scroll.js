// Fixed bounded scroll. No navigation, click, form read, or caller parameter.
() => {
  const result = state => ({schema_version:1,state,cards:[],scroll_y:Math.max(0,Number.isFinite(scrollY)?scrollY:0),viewport_height:Math.max(1,Number.isFinite(innerHeight)?innerHeight:1)});
  const shown = el => {
    for(let p=el;p;p=p.parentElement) {
      const style=getComputedStyle(p);
      if(p.hidden || p.getAttribute('aria-hidden')==='true' || style.display==='none' || style.visibility==='hidden' || style.visibility==='collapse' || Number(style.opacity)===0 || style.contentVisibility==='hidden') return false;
    }
    return [...el.getClientRects()].some(r=>r.width>0 && r.height>0 && r.right>0 && r.bottom>0 && r.left<innerWidth && r.top<innerHeight);
  };
  if (['login','challenge','checkpoint'].some(part=>location.pathname.split('/').includes(part)) || [...document.querySelectorAll('[role=dialog],form input[type=password],[role=alert][aria-label="Account warning"]')].some(shown)) return result('manual_login_or_challenge');
  if (location.protocol!=='https:' || !['www.threads.com','threads.com','www.threads.net','threads.net'].includes(location.hostname) || location.port || location.search || location.hash || location.pathname!=='/') return result('unexpected_navigation');
  window.scrollBy({top:Math.max(1,Math.floor(innerHeight*0.65)),behavior:'instant'});
  return result('ready');
}
