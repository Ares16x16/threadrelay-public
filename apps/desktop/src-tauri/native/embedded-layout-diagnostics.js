// Debug-only fixed metadata diagnostic. Never wire to production worker controls.
// Mirrors snapshotCard checks; raw successful values stay in-memory and are discarded.
// Output is a closed bounded schema: counts, booleans, and static reason enum only.
() => {
    const selector='div[data-pressable-container="true"][data-interactive-id=""],main [role=feed]>article';
    const boundary='article,blockquote,div[data-pressable-container="true"][data-interactive-id=""]';
    const cap = value => Math.min(101, value);
    const candidates=[...document.querySelectorAll(selector)];
    const output={schema_version:1,candidate_count:cap(candidates.length),candidate_limit:candidates.length>100,sampled_count:0,samples:[]};
    const shown=el=>{
        for(let p=el;p;p=p.parentElement) {
            const s=getComputedStyle(p);
            if(p.hidden || p.getAttribute('aria-hidden')==='true' || s.display==='none' ||
                s.visibility==='hidden' || s.visibility==='collapse' || Number(s.opacity)===0 || s.contentVisibility==='hidden') return false;
        }
        return [...el.getClientRects()].some(r=>r.width>0 && r.height>0 && r.right>0 && r.bottom>0 && r.left<innerWidth && r.top<innerHeight);
    };
    const finite2dTranslation = transform => {
        if (!transform || transform==='none') return true;
        const matrix=transform.match(/^matrix\(([^()]*)\)$/);
        if(!matrix) return false;
        const parts=matrix[1].split(',').map(value=>value.trim());
        if(parts.length!==6 || parts.some(value=>!/^[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?$/i.test(value))) return false;
        const values=parts.map(Number);
        return values.every(Number.isFinite) && values[0]===1 && values[1]===0 && values[2]===0 && values[3]===1;
    };
    let authorChecks;
    const recordBodyMetadata=(content,bodies,text)=>{
        const tag=value=>{const n=['SPAN','DIV','A','BUTTON'].indexOf(value);return n<0?9:n;};
        const role=value=>{const n=[null,'button','link'].indexOf(value);return n<0?9:n;};
        authorChecks.body_candidate_count=cap(bodies.length);
        authorChecks.body_common_parent=bodies.length>1 && bodies.every(el=>el.parentElement===bodies[0].parentElement);
        authorChecks.body_geometry=bodies.slice(0,6).map(el=>{
            const raw=el.textContent??'', own=text(el);
            const label=raw.length<=100?raw.trim().toLowerCase():'';
            const ui=['translate','see translation','show more','see more','翻譯','翻译','查看翻譯','查看翻译'].indexOf(label);
            const path=[];for(let p=el;p && p!==content && path.length<101;p=p.parentElement)path.push([...p.parentElement.children].indexOf(p));
            const branch=path.slice().reverse().slice(0,4);while(branch.length<4)branch.push(-1);
            const control=el.closest('button,input,textarea,select,nav,[role=button],[role=link],a');
            const nearest=control?(control.matches('button,[role=button]')?1:control.matches('a,[role=link]')?2:3):0;
            const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let found=false,all=true,seen=0;
            for(let n=walker.nextNode();n;n=walker.nextNode()){
                if(++seen>101){all=false;break;}if(!n.textContent.trim())continue;found=true;
                const c=n.parentElement.closest('button,input,textarea,select,nav,[role=button]');
                if(!c || !el.contains(c))all=false;
            }
            return [own===null?100001:Math.min(100001,own.length),Math.min(100001,raw.length),ui<0?9:ui,
                tag(el.parentElement.tagName),role(el.parentElement.getAttribute('role')),nearest,
                cap(el.querySelectorAll('button,input,textarea,select,nav,[role=button]').length),found&&all?1:0,cap(path.length),...branch];
        });
    };
    const recordAuthorMetadata=(header, headerLinks, authors, platformUrl, boundary, visible, displayed, unsupportedTextLayout, nonDefault, clips)=>{
        const ownLinks=[...header.querySelectorAll('a[role=link][href]')].filter(a=>a.closest(boundary)===header.closest(boundary));
        const platform=ownLinks.filter(a=>platformUrl(a));
        const paths=platform.filter(a=>new RegExp('^/@[A-Za-z0-9_.]{1,30}/?$').test(platformUrl(a).pathname));
        const exact=paths.filter(a=>{const u=platformUrl(a);return !u.search && !u.hash;});
        Object.assign(authorChecks, {
            own_header_link_count:cap(ownLinks.length), platform_link_count:cap(platform.length),
            profile_path_count:cap(paths.length), profile_exact_count:cap(exact.length),
            profile_displayed_count:cap(exact.filter(displayed).length),
            profile_visible_count:cap(exact.filter(visible).length),
            profile_layout_supported_count:cap(exact.filter(a=>visible(a) && !unsupportedTextLayout(a)).length),
            filtered_header_link_count:cap(headerLinks.length), filtered_author_count:cap(authors.length)});
        const timeNodes=[...header.querySelectorAll('time')];
        const timeLinks=ownLinks.filter(a=>a.querySelector('time'));
        Object.assign(authorChecks, {
            time_link_count:cap(timeLinks.length),
            time_link_visible_count:cap(timeLinks.filter(visible).length),
            time_link_layout_count:cap(timeLinks.filter(a=>visible(a) && !unsupportedTextLayout(a)).length),
            time_link_filtered_count:cap(timeLinks.filter(a=>headerLinks.includes(a)).length),
            time_node_displayed_count:cap(timeNodes.filter(displayed).length),
            time_node_visible_count:cap(timeNodes.filter(visible).length),
            time_display_contents_count:cap(timeNodes.filter(t=>getComputedStyle(t).display==='contents').length),
            time_anchor_present:timeNodes.some(t=>t.closest('a[href]')),
            time_anchor_role:timeNodes.some(t=>t.closest('a[role=link][href]')),
            time_hidden:false,time_viewport_clipped:false,time_self_overflow:false,
            time_ancestor_clipped:false,time_nontranslation:false,time_mask_clip_filter:false,
            clip_x_subpixel:false,clip_y_subpixel:false,clip_inline:false,clip_zero_client:false,clip_geometry:[]});
        const clipRecorded=new Set();
        const metric=value=>Number.isFinite(value)?Math.max(-10000,Math.min(10000,Math.round(value*1000)/1000)):null;
        const code=(value,values)=>{const index=values.indexOf(value);return index<0?9:index;};
        for(const t of [...timeLinks,...timeNodes].slice(0,101)) {
            const glyphRects=[];
            const walker=document.createTreeWalker(t,NodeFilter.SHOW_TEXT);
            for(let n=walker.nextNode(),seen=0;n && seen<101;n=walker.nextNode(),seen++) {
                if(!n.textContent.trim() || !displayed(n.parentElement)) continue;
                const range=document.createRange();range.selectNodeContents(n);
                if(typeof range.getClientRects==='function') glyphRects.push(...[...range.getClientRects()].filter(r=>r.width>0 && r.height>0).slice(0,101));
                range.detach();
            }
            for(let p=t;p;p=p.parentElement) {
                const s=getComputedStyle(p);
                if(p.hidden || p.getAttribute('aria-hidden')==='true' || s.display==='none' ||
                    ['hidden','collapse'].includes(s.visibility) || Number(s.opacity)===0) authorChecks.time_hidden=true;
                if(!finite2dTranslation(s.transform)) authorChecks.time_nontranslation=true;
                if(nonDefault(s.maskImage,'none') || nonDefault(s.webkitMaskImage,'none') ||
                    nonDefault(s.clipPath,'none') || nonDefault(s.clip,'auto') || nonDefault(s.filter,'none')) authorChecks.time_mask_clip_filter=true;
            }
            for(const r of [...t.getClientRects()].slice(0,101)) {
                if(r.left<0 || r.top<0 || r.right>innerWidth || r.bottom>innerHeight) authorChecks.time_viewport_clipped=true;
                const style=getComputedStyle(t);
                if((clips(style.overflowX) && t.scrollWidth>t.clientWidth+1) ||
                    (clips(style.overflowY) && t.scrollHeight>t.clientHeight+1)) authorChecks.time_self_overflow=true;
                for(let p=t.parentElement;p;p=p.parentElement) {
                    const s=getComputedStyle(p),box=p.getBoundingClientRect(),left=box.left+p.clientLeft,top=box.top+p.clientTop;
                    const dx=clips(s.overflowX)?Math.max(0,left-r.left,r.right-left-p.clientWidth):0;
                    const dy=clips(s.overflowY)?Math.max(0,top-r.top,r.bottom-top-p.clientHeight):0;
                    if(dx>0 || dy>0) {
                        authorChecks.time_ancestor_clipped=true;
                        if(dx>0 && dx<=1) authorChecks.clip_x_subpixel=true;
                        if(dy>0 && dy<=1) authorChecks.clip_y_subpixel=true;
                        if(s.display==='inline') authorChecks.clip_inline=true;
                        if((dx>0 && p.clientWidth===0) || (dy>0 && p.clientHeight===0)) authorChecks.clip_zero_client=true;
                        if(dy>0 && authorChecks.clip_geometry.length<3 && !clipRecorded.has(p)) {
                            clipRecorded.add(p);
                            authorChecks.clip_geometry.push([
                                code(p.tagName,['DIV','SPAN','A','BODY','MAIN','SECTION','HTML']),
                                code(s.display,['block','inline','inline-block','flex','inline-flex','grid']),
                                code(s.overflowX,['visible','hidden','clip','auto','scroll']),
                                code(s.overflowY,['visible','hidden','clip','auto','scroll']),
                                code(s.scrollbarGutter,['auto','stable','stable both-edges']),
                                r.top-box.top,r.bottom-box.top,box.height,p.clientHeight,p.offsetHeight,p.clientTop,
                                Number.parseFloat(s.borderTopWidth),Number.parseFloat(s.borderBottomWidth),p.scrollHeight,dx,dy,
                                glyphRects.length?Math.min(...glyphRects.map(g=>g.top-box.top)):0,
                                glyphRects.length?Math.max(...glyphRects.map(g=>g.bottom-box.top)):0,cap(glyphRects.length)
                            ].map(metric));
                        }
                    }
                }
            }
        }
        for(const a of exact.slice(0,101)) for(const r of [...a.getClientRects()].slice(0,101)) {
            if(r.left<0 || r.top<0 || r.right>innerWidth || r.bottom>innerHeight) authorChecks.profile_viewport_clipped=true;
            for(let p=a;p;p=p.parentElement) {
                const style=getComputedStyle(p);
                if(nonDefault(style.transform,'none')) authorChecks.profile_transform=true;
                if(!finite2dTranslation(style.transform)) authorChecks.profile_transform_nontranslation=true;
                if(nonDefault(style.maskImage,'none') || nonDefault(style.webkitMaskImage,'none')) authorChecks.profile_mask=true;
                if(nonDefault(style.clipPath,'none') || nonDefault(style.clip,'auto')) authorChecks.profile_clip=true;
                if(nonDefault(style.filter,'none')) authorChecks.profile_filter=true;
                if(nonDefault(style.contentVisibility,'visible')) authorChecks.profile_content_visibility=true;
                if(p===a) {
                    if((clips(style.overflowX) && p.scrollWidth>p.clientWidth+1) || (clips(style.overflowY) && p.scrollHeight>p.clientHeight+1)) authorChecks.profile_self_overflow=true;
                } else {
                    const box=p.getBoundingClientRect(), left=box.left+p.clientLeft, top=box.top+p.clientTop;
                    if((clips(style.overflowX) && (r.left<left || r.right>left+p.clientWidth)) || (clips(style.overflowY) && (r.top<top || r.bottom>top+p.clientHeight))) authorChecks.profile_ancestor_clipped=true;
                }
            }
        }
    };
    // BEGIN EXACT MIRROR WITH STATIC ISSUE LABELS
    const snapshotCard = (card) => {
    const issue = code => ({issue: code});
    const hasLayout = (el, allowZeroWidth) => {
        if (!el || !el.isConnected) return false;
        for (let p=el; p; p=p.parentElement) {
            const s=getComputedStyle(p);
            if (p.hidden || p.getAttribute('aria-hidden')==='true' ||
                s.display==='none' || s.visibility==='hidden' || s.visibility==='collapse' ||
                Number(s.opacity)===0 || s.contentVisibility==='hidden') return false;
        }
        return [...el.getClientRects()].some(r=>(r.width>0 || (allowZeroWidth && r.width===0)) && r.height>0);
    };
    const displayed = el => hasLayout(el, false);
    const nonDefault = (value, normal) => value && value!==normal;
    // Only the observed translation/identity case preserves axis-aligned
    // evidence boxes. Scaling, skew, rotation and 3D stay unsupported.
    const finite2dTranslation = transform => {
        if (!transform || transform==='none') return true;
        const matrix=transform.match(/^matrix\(([^()]*)\)$/);
        if(!matrix) return false;
        const parts=matrix[1].split(',').map(value=>value.trim());
        if(parts.length!==6 || parts.some(value=>!/^[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?$/i.test(value))) return false;
        const values=parts.map(Number);
        return values.every(Number.isFinite) && values[0]===1 && values[1]===0 && values[2]===0 && values[3]===1;
    };
    const unsupportedCss = s => nonDefault(s.clipPath,'none') || nonDefault(s.clip,'auto') ||
        nonDefault(s.filter,'none') || nonDefault(s.maskImage,'none') ||
        nonDefault(s.webkitMaskImage,'none') || nonDefault(s.contentVisibility,'visible') ||
        !finite2dTranslation(s.transform);
    const clips = value => ['hidden','clip','auto','scroll','overlay'].includes(value);
    const clipBox = (el, style) => {
        // The standards-mode root scrollport is the viewport. Its document
        // bounding box moves on scroll; client dimensions remain viewport-sized.
        if(el===document.documentElement && document.compatMode!=='BackCompat')
            return {left:0,top:0,right:el.clientWidth,bottom:el.clientHeight};
        const r=el.getBoundingClientRect();
        const oldLeft=r.left+el.clientLeft, oldTop=r.top+el.clientTop;
        const borders=[style.borderLeftWidth,style.borderRightWidth,style.borderTopWidth,style.borderBottomWidth].map(Number.parseFloat);
        // Client/offset dimensions round to integers. When their difference
        // equals the complete finite border width, no gutter is allocated on
        // that axis: use the precise near/far padding edges inside BCR. Keep
        // the conservative client box on mismatch; never add pixel tolerance.
        const finite=value=>Number.isFinite(value) && value>=0;
        const exactX=style.display!=='inline' && el.clientWidth>0 && borders.slice(0,2).every(finite) &&
            el.offsetWidth-el.clientWidth===borders[0]+borders[1];
        const exactY=style.display!=='inline' && el.clientHeight>0 && borders.slice(2,4).every(finite) &&
            el.offsetHeight-el.clientHeight===borders[2]+borders[3];
        return {left:exactX?r.left+borders[0]:oldLeft,top:exactY?r.top+borders[2]:oldTop,
            right:exactX?r.right-borders[1]:oldLeft+el.clientWidth,
            bottom:exactY?r.bottom-borders[3]:oldTop+el.clientHeight};
    };
    // Strong evidence gate: a positive rect alone cannot establish identity or
    // audience. Require the complete evidence rect within viewport and every
    // ancestor clip box, and reject unsupported masks/CSS on any ancestor.
    const supportedRects = (el, inputRects, allowZeroWidth, textRange=false) => {
        if (!hasLayout(el, allowZeroWidth)) return false;
        const rects=inputRects.filter(r=>(r.width>0 || (allowZeroWidth && r.width===0)) && r.height>0);
        if (!rects.length || rects.some(r=>![r.left,r.top,r.right,r.bottom,r.width,r.height].every(Number.isFinite))) return false;
        for (const r of rects) {
            if(r.left<0 || r.top<0 || r.right>innerWidth || r.bottom>innerHeight ||
               (allowZeroWidth && r.width===0 && r.left>=innerWidth)) return false;
            for(let p=el;p;p=p.parentElement) {
                const s=getComputedStyle(p);
                if(unsupportedCss(s)) return false;
                if(p===el && !textRange) {
                    if((clips(s.overflowX) && p.scrollWidth>p.clientWidth+1) ||
                       (clips(s.overflowY) && p.scrollHeight>p.clientHeight+1)) return false;
                    continue;
                }
                const box=clipBox(p,s);
                if((clips(s.overflowX) && (r.left<box.left || r.right>box.right || (allowZeroWidth && r.width===0 && r.left>=box.right))) ||
                   (clips(s.overflowY) && (r.top<box.top || r.bottom>box.bottom))) return false;
            }
        }
        return true;
    };
    const supported = (el, allowZeroWidth) => supportedRects(el, el?[...el.getClientRects()]:[], allowZeroWidth);
    const visible = el => supported(el, false);
    // A rendered BR may have a zero-width but positive-height line box. It
    // still needs all viewport, clipping, style and ancestor evidence checks.
    const visibleBreak = el => el.tagName==='BR' && supported(el, true);
    const own = (root, selector) => [...root.querySelectorAll(selector)]
        .filter(el => el.closest('article, blockquote')===root && displayed(el));
    const clipped = el => {
        for (let p=el;p && p!==card.parentElement;p=p.parentElement) {
            const s=getComputedStyle(p);
            if ((['hidden','clip'].includes(s.overflowY) && p.scrollHeight>p.clientHeight+1) ||
                (['hidden','clip'].includes(s.overflowX) && p.scrollWidth>p.clientWidth+1)) return true;
        }
        return false;
    };
    const unsupportedTextLayout = el => [el, ...el.querySelectorAll('*')].some(child => {
        if (!hasLayout(child, child.tagName==='BR') || child.matches('button,input,textarea,select,nav,script,style,template,[role=button]')) return false;
        const s=getComputedStyle(child), r=child.getBoundingClientRect();
        return !(child.tagName==='BR' ? visibleBreak(child) : visible(child)) || unsupportedCss(s) ||
            r.left<0 || r.top<0 || r.right>innerWidth || r.bottom>innerHeight || clipped(child);
    });
    // Header line boxes may include clipped leading while every text Range
    // still fits. Verify each rendered text node against the same exact clip,
    // viewport and ancestor-style gates; never use a pixel allowance.
    const textNodeSupported = node => {
        const parent=node.parentElement;
        if(!displayed(parent)) return false;
        if(!node.textContent.trim()) return true;
        try {
            const range=document.createRange();
            if(typeof range.getClientRects!=='function') return false;
            range.selectNodeContents(node);
            const rects=[...range.getClientRects()];
            range.detach();
            return supportedRects(parent,rects,false,true);
        } catch { return false; }
    };
    const headerTextSupported = el => {
        if(!displayed(el)) return false;
        if(visible(el) && !unsupportedTextLayout(el)) return true;
        const elements=[el,...el.querySelectorAll('*')];
        if(elements.length>100 || elements.some(child=>(displayed(child) && unsupportedCss(getComputedStyle(child))) || (child.tagName==='BR' && hasLayout(child,true) && !visibleBreak(child)))) return false;
        const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
        let count=0, characters=0, found=false;
        for(let node=walker.nextNode();node;node=walker.nextNode()) {
            if(++count>100) return false;
            const parent=node.parentElement;
            if(!displayed(parent)) continue;
            const control=parent.closest('blockquote,article,button,input,textarea,select,nav,script,style,template,[role=button]');
            if(control && el.contains(control)) continue;
            characters+=node.textContent.length;
            if(characters>2000 || !textNodeSupported(node)) return false;
            if(node.textContent.trim()) found=true;
        }
        return found;
    };
    const text = (el, headerMode=false) => {
        const ranges=headerMode===true && !(visible(el) && !unsupportedTextLayout(el));
        if(ranges && !headerTextSupported(el)) return null;
        let out=''; let tooLong=false;
        const walk = node => {
            if (tooLong) return;
            if (node.nodeType===Node.TEXT_NODE) {
                if (ranges?textNodeSupported(node):visible(node.parentElement)) out+=node.textContent;
            } else if (node.nodeType===Node.ELEMENT_NODE && (node.tagName==='BR' ? visibleBreak(node) : ranges?displayed(node):visible(node))) {
                if (node!==el && node.matches('blockquote,article,button,input,textarea,select,nav,script,style,template,[role=button]')) return;
                if (node.tagName==='BR') out+='\n';
                else {
                    const block=node!==el && ['block','list-item'].includes(getComputedStyle(node).display);
                    if(block && out && !out.endsWith('\n')) out+='\n';
                    for(const child of node.childNodes) walk(child);
                    if(block && out && !out.endsWith('\n')) out+='\n';
                }
            }
            if(out.length>100000) tooLong=true;
        };
        walk(el);
        return tooLong ? null : out.replace(/[ \t]+/g,' ').replace(/ *\n */g,'\n').trim();
    };
    // Expanded state also belongs to menus, comments and media viewers.
    // A rendered control needs an explicit text-expansion label, including
    // its own accessible label when it contains only an icon.
    const collapsedTextExpansion = el => {
        if(!visible(el) || unsupportedTextLayout(el)) return false;
        const popup=el.getAttribute('aria-haspopup');
        if(popup && popup!=='false') return false;
        return [text(el),el.getAttribute('aria-label')].some(label=>typeof label==='string' &&
            /^(?:read more|show more|see more|continue reading|顯示更多|显示更多|查看更多|閱讀更多|阅读更多|展開全文|展开全文|查看全文)$/i.test(label.trim()));
    };
    const publicSelector = 'div[data-pressable-container="true"][data-interactive-id=""]';
    const boundary = 'article,blockquote,' + publicSelector;
    const inViewport = el => [...el.getClientRects()].some(r => r.width>0 && r.height>0 && r.right>0 && r.bottom>0 && r.left<innerWidth && r.top<innerHeight);
    const attention = [...document.querySelectorAll('[role=dialog],form input[type=password],[role=alert][aria-label="Account warning"]')]
        .some(el => displayed(el) && inViewport(el));
    if (attention) return {issue:'unknown_layout',diagnosticReason:'page_attention'};
    if (!card || !displayed(card)) return {issue:'not_visible',diagnosticReason:'not_displayed'};
    if (!card.matches('article') && !card.matches(publicSelector)) return {issue:'unknown_layout',diagnosticReason:'unrecognized_card'};
    const rect=card.getBoundingClientRect();
    if (rect.bottom<=0 || rect.top>=innerHeight || rect.right<=0 || rect.left>=innerWidth) return {issue:'not_visible',diagnosticReason:'outside_viewport'};
    // Optional metadata comes only from supported rendered evidence in this
    // author boundary. Missing values stay absent; rounded labels are not counts.
    const cleanPublicReference = value => {
        try {
            const u=new URL(value), host=u.hostname.toLowerCase().replace(/\.+$/,'');
            if(u.protocol!=='https:' || u.username || u.password || u.port || u.search || u.hash ||
                value.length>4096 || !host.includes('.') || /^[0-9.]+$/.test(host) || host.includes(':') ||
                ['.localhost','.local','.internal','.invalid','.test','.home.arpa'].some(suffix=>host.endsWith(suffix))) return null;
            return u.href;
        } catch { return null; }
    };
    const observedMetadata = (root, header, body, time, mediaRoot) => {
        const result={};
        const ownEvidence=el=>el.closest(boundary)===root && visible(el) && !unsupportedTextLayout(el);
        const names=[...header.querySelectorAll('[itemprop="name"]')].filter(ownEvidence);
        if(names.length===1) {
            const value=text(names[0]);
            if(value && value.length<=500) result.display_name=value;
        }
        if(time && time.closest(boundary)===root && headerTextSupported(time)) {
            const label=text(time,true), value=time.getAttribute('datetime');
            if(label && label.length<=200) result.published_label=label;
            if(value && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))) result.published_at=value;
        }
        const actions=[['likes',['Like','Likes','讚','讚好','赞','喜欢','喜歡']],
            ['replies',['Comment','Comments','Reply','Replies','回覆','回复','留言']],
            ['reposts',['Repost','Reposts','轉發','转发','轉貼']],
            ['quotes',['Quote','Quotes','引用']],['views',['View','Views','瀏覽','浏览']],
            ['shares',['Share','Shares','分享']]];
        const actionKind=label=>actions.find(([,names])=>names.some(name=>label.toLowerCase()===name.toLowerCase()))?.[0];
        const engagement=[];
        for(const control of [...root.querySelectorAll('button,[role=button]')].slice(0,101)) {
            if(!ownEvidence(control) || (body && body.contains(control))) continue;
            const label=text(control);
            if(!label || label.length>200) continue;
            let kind, numberLabel=label;
            for(const [candidate,names] of actions) for(const name of names) {
                if(label.toLowerCase()===name.toLowerCase() || label.toLowerCase().startsWith(name.toLowerCase()+' ')) {
                    kind=candidate; numberLabel=label.slice(name.length).trim();
                }
            }
            if(!kind) {
                const accessible=[control,...control.querySelectorAll('[aria-label]')].filter(visible)
                    .map(el=>el.getAttribute('aria-label')).filter(Boolean).map(actionKind).filter(Boolean);
                if(accessible.length===1) kind=accessible[0];
            }
            if(!kind) continue;
            const entry={kind,label};
            if(/^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)$/.test(numberLabel)) {
                const count=Number(numberLabel.replace(/,/g,''));
                if(Number.isSafeInteger(count)) entry.count=count;
            }
            engagement.push(entry);
        }
        const unique=engagement.filter(entry=>engagement.filter(other=>other.kind===entry.kind).length===1);
        if(unique.length) result.engagement=unique.slice(0,20);
        const media=[];
        for(const el of [...mediaRoot.querySelectorAll('img,video,a[href]')].slice(0,101)) {
            if(el.closest(boundary)!==root || !visible(el) || el.closest('header,footer,nav')) continue;
            // Real attachments remain media inside a pressable viewer. Its
            // labels/links are controls, never post text or model descriptions.
            if(el.closest('button,[role=button]') && !el.matches('img,video')) continue;
            if(root.matches('article,blockquote') && el.closest('button') && !el.closest('[aria-label=Attachment]')) continue;
            const kind=el.tagName==='IMG'?'image':el.tagName==='VIDEO'?'video':'link';
            const entry={kind};
            const url=cleanPublicReference(kind==='link'?el.href:(el.currentSrc || el.src));
            if(url) entry.url=url;
            if(kind==='link' && !url) continue;
            if(kind==='image') {
                const alt=el.getAttribute('alt');
                if(alt && alt.length<=2000) entry.alt=alt;
            }
            media.push(entry);
            if(media.length>=20) break;
        }
        if(media.length) result.media=media;
        return result;
    };
    const parse = root => {
        const headers=own(root, ':scope > header');
        const bodies=own(root, ':scope > section[aria-label="Post text"]');
        if(headers.length!==1 || bodies.length!==1) return {issue:'unknown_layout',diagnosticReason:'semantic_columns'};
        const header=headers[0], body=bodies[0];
        // header's nearest article/quote is the same root, not the header.
        const headerOwn = (selector,textual=false) => [...header.querySelectorAll(selector)]
            .filter(el=>el.closest('article,blockquote')===root && (textual?headerTextSupported(el):visible(el) && !unsupportedTextLayout(el)));
        const as=headerOwn('a[rel~="author"]',true);
        if(as.length!==1) return {issue:'unknown_layout',diagnosticReason:'semantic_author_count'};
        const author=text(as[0],true);
        const links=headerOwn('a[href]',true).filter(a=>[...a.querySelectorAll('time')].some(headerTextSupported));
        if(links.length!==1) return {issue:'missing_permalink',diagnosticReason:'semantic_permalink_count'};
        if(clipped(body)) return {issue:'text_clipped',diagnosticReason:'semantic_text_clipped'};
        if(unsupportedTextLayout(body)) return {issue:'unsupported_text_layout',diagnosticReason:'semantic_text_layout'};
        const bodyRect=body.getBoundingClientRect();
        if(bodyRect.top<0 || bodyRect.bottom>innerHeight || bodyRect.left<0 || bodyRect.right>innerWidth) return {issue:'partially_visible',diagnosticReason:'semantic_partial_text'};
        const bodyText=text(body);
        if(author===null || bodyText===null) return {issue:'text_too_large',diagnosticReason:'semantic_text_limit'};
        const audience=headerOwn('[role=note][aria-label=Audience]').map(el=>text(el));
        const truncated=own(root, 'button[aria-expanded="false"], [role=button][aria-expanded="false"]').some(collapsedTextExpansion) || /[…]|\.{3}$/.test(bodyText);
        return {author, author_url:as[0].href, permalink:links[0].href,
                text:bodyText, audience, truncated, ...(root===card?{observed_metadata:observedMetadata(root,header,body,links[0].querySelector('time'),root)}:{})};
    };
    // Presence only: never read media URLs/bytes/alt contents or infer meaning.
    // Scope by nearest author boundary, excluding avatars and action icons.
    const mediaPresent = root => [...root.querySelectorAll(
        'img,video,audio,canvas,object,embed,iframe,figure,[role=img],[aria-label=Attachment],a[download]'
    )].some(el => {
        if(el.closest('article,blockquote')!==root ||
           el.closest('header,footer,nav') || !displayed(el)) return false;
        if(el.closest('button,[role=button]') && !el.matches('img,video,audio,canvas,object,embed,iframe,figure')) return false;
        // Semantic action buttons may use IMG icons. A native button needs
        // explicit attachment evidence; observed public viewer DIVs are separate.
        if(el.closest('button') && !el.closest('[aria-label=Attachment]')) return false;
        return [...el.getClientRects()].some(r=>r.width>0 && r.height>0 &&
            r.right>0 && r.bottom>0 && r.left<innerWidth && r.top<innerHeight);
    });
    if (card.matches(publicSelector)) {
        // The public adapter recognizes the observed avatar/header/content
        // columns. It does not infer post text from a whole-card text fallback.
        if (card.parentElement.closest(boundary)) return {issue:'unknown_layout',diagnosticReason:'public_nested_card'};
        const columns=card.firstElementChild;
        if (!columns || columns.tagName!=='DIV' || columns.children.length!==3 ||
            ![...columns.children].every(el=>el.tagName==='DIV')) return {issue:'unknown_layout',diagnosticReason:'public_columns'};
        const [avatar,header,content]=columns.children;
        if (!displayed(header) || !displayed(content)) return {issue:'not_visible',diagnosticReason:'public_columns_hidden'};
        if ([...content.querySelectorAll(boundary)].some(displayed)) return {issue:'unknown_layout',diagnosticReason:'public_nested_boundary'};
        const platformUrl = a => {
            try {
                const u=new URL(a.href);
                return u.protocol==='https:' && ['www.threads.com','threads.com','www.threads.net','threads.net'].includes(u.hostname) &&
                    !u.username && !u.password && !u.port ? u : null;
            } catch { return null; }
        };
        const headerLinks=[...header.querySelectorAll('a[role=link][href]')]
            .filter(a=>a.closest(boundary)===card && headerTextSupported(a));
        const authors=headerLinks.filter(a=>{
            const u=platformUrl(a);
            return u && !u.search && !u.hash && new RegExp('^/@[A-Za-z0-9_.]{1,30}/?$').test(u.pathname);
        });
        recordAuthorMetadata(header, headerLinks, authors, platformUrl, boundary, visible, displayed, unsupportedTextLayout, nonDefault, clips);
        if (authors.length!==1) return {issue:'unknown_layout',diagnosticReason:'public_author_count'};
        const authorLink=authors[0], profile=platformUrl(authorLink);
        const handle=profile.pathname.split('/')[1];
        const label=text(authorLink,true);
        if (label===null || label.replace(/^@/,'').toLowerCase()!==handle.slice(1).toLowerCase()) return {issue:'unknown_layout',diagnosticReason:'public_author_label'};
        const times=headerLinks.filter(a=>[...a.querySelectorAll('time')].filter(headerTextSupported).length===1);
        if (times.length!==1) return {issue:'missing_permalink',diagnosticReason:'public_time_count'};
        const permalink=platformUrl(times[0]);
        const match=permalink && permalink.pathname.match(new RegExp('^/(@[A-Za-z0-9_.]{1,30})/post/[A-Za-z0-9_-]{1,128}/?$'));
        if (!match || match[1].toLowerCase()!==handle.toLowerCase()) return {issue:'unknown_layout',diagnosticReason:'public_permalink_identity'};
        // A second post identity inside content is an unsupported quote/repost,
        // even if the platform did not expose a semantic blockquote boundary.
        if ([...content.querySelectorAll('a[href]')].some(a=>{
            const u=platformUrl(a);
            return displayed(a) && u && new RegExp('^/@[A-Za-z0-9_.]{1,30}/post/').test(u.pathname);
        })) return {issue:'unknown_layout',diagnosticReason:'public_content_post_link'};
        const media=[...content.querySelectorAll('img,video,audio,canvas,object,embed,iframe,figure,[role=img],[aria-label=Attachment],a[download]')]
            .some(el=>{
                if(el.closest(boundary)!==card || !displayed(el) || !inViewport(el)) return false;
                // Avatar is outside content. SVG/button action icons are not
                // post media; actual image/video elements remain conservatively
                // media even when wrapped in a pressable viewer control.
                const control=el.closest('button,[role=button]');
                return !control || !content.contains(control) || el.matches('img,video,audio,canvas,object,embed,iframe,figure');
            });
        const bodies=[...content.querySelectorAll('span[dir=auto]')].filter(el=>{
            if (!displayed(el) || el.closest(boundary)!==card) return false;
            const control=el.closest('button,input,textarea,select,nav,[role=button]');
            if (control && content.contains(control)) return false;
            for(let parent=el.parentElement;parent && parent!==content;parent=parent.parentElement) {
                if(parent.matches('span[dir=auto]')) return false;
            }
            return true;
        });
        recordBodyMetadata(content,bodies,text);
        if (bodies.length>1 || (!bodies.length&&!media)) return {issue:'unknown_layout',diagnosticReason:'public_body_count'};
        // A recognized media-only card has no caption to invent or read.
        const body=bodies[0]??null;
        if (body && clipped(body)) return {issue:'text_clipped',diagnosticReason:'public_text_clipped'};
        if (body && unsupportedTextLayout(body)) return {issue:'unsupported_text_layout',diagnosticReason:'public_text_layout'};
        const bodyText=body?text(body):'';
        if(bodyText===null) return {issue:'text_too_large',diagnosticReason:'public_text_limit'};
        const audience=[...header.querySelectorAll('[role=note][aria-label=Audience]')]
            .filter(el=>el.closest(boundary)===card && visible(el) && !unsupportedTextLayout(el)).map(el=>text(el));
        const pagePlatform=location.protocol==='https:' && ['www.threads.com','threads.com','www.threads.net','threads.net'].includes(location.hostname) && !location.port;
        const links=[...document.querySelectorAll('a[href]')];
        // This is evidence of rendered signed-out public access, not a Public
        // badge. A link inside any post can never establish page auth context.
        const guestPublic=pagePlatform && links.length<=2000 && links.some(a=>{
            if(a.closest(boundary) || !visible(a) || unsupportedTextLayout(a)) return false;
            const u=platformUrl(a);
            return u && new RegExp('^/login/?$').test(u.pathname) && /^(?:log in|log in with username instead)$/i.test(text(a));
        });
        const truncated=[...content.querySelectorAll('button[aria-expanded="false"],[role=button][aria-expanded="false"]')].some(collapsedTextExpansion) || /[…]|\.{3}$/.test(bodyText);
        return {author:handle,author_url:authorLink.href,permalink:times[0].href,text:bodyText,audience,truncated,
            quote:null,quote_count:0,nested_quote:false,missing_media_context:media,quote_missing_media_context:false,
            guest_public_context:guestPublic, observed_metadata:observedMetadata(card,header,body,times[0].querySelector('time'),content)};
    }
    const outer=parse(card); if(outer.issue) return outer;
    const quotes=[...card.querySelectorAll('blockquote')].filter(q=>q.parentElement.closest('article,blockquote')===card && displayed(q));
    const quote=quotes.length===1 ? parse(quotes[0]) : null;
    return {...outer, quote, quote_count:quotes.length, missing_media_context:mediaPresent(card),
            quote_missing_media_context:quotes.some(mediaPresent),
            nested_quote:quotes.length===1 && [...quotes[0].querySelectorAll('blockquote')].some(displayed)};
};
    // END EXACT MIRROR WITH STATIC ISSUE LABELS
    for(const card of candidates.slice(0,101)) {
        if(output.samples.length>=4) break;
        if(card.parentElement.closest(boundary) || !shown(card)) continue;
        const columns=card.firstElementChild;
        const header=columns?.children[1], content=columns?.children[2];
        authorChecks={own_header_link_count:0,platform_link_count:0,profile_path_count:0,profile_exact_count:0,profile_displayed_count:0,profile_visible_count:0,profile_layout_supported_count:0,filtered_header_link_count:0,filtered_author_count:0,profile_transform:false,profile_transform_nontranslation:false,profile_mask:false,profile_clip:false,profile_filter:false,profile_content_visibility:false,profile_viewport_clipped:false,profile_self_overflow:false,profile_ancestor_clipped:false};
        const sample={author_checks:authorChecks,reason:'diagnostic_error',recognized:false,
            column_count:cap(columns?.children.length??0),
            header_anchor_count:cap(header?.querySelectorAll('a[role=link][href]').length??0),
            header_time_count:cap(header?.querySelectorAll('time').length??0),
            content_span_count:cap(content?.querySelectorAll('span[dir=auto]').length??0),
            nested_boundary_count:cap(content?.querySelectorAll(boundary).length??0)};
        try {
            const raw=snapshotCard(card);
            sample.recognized=!raw.issue;
            sample.reason=sample.recognized?'recognized':raw.diagnosticReason;
        } catch { /* Never expose exception strings or page-controlled values. */ }
        output.samples.push(sample);
    }
    output.sampled_count=output.samples.length;
    return output;
}
