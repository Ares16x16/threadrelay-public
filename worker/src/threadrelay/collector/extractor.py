"""Atomic, bounded rendered-DOM snapshots with explicit author/quote boundaries.

Supports the semantic fixture adapter and a narrowly recognized public DIV
layout observed on a signed-out Threads feed. Rendered platform login controls
can establish guest access context; absence of audience/login evidence cannot.
Unsupported layouts fail closed. No hydration, hidden text or network API reads.
"""
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import hashlib
import json
from typing import Any, NotRequired, TypedDict
from urllib.parse import urlsplit

from playwright.sync_api import Locator

from threadrelay.collector.canonicalize import HOSTS, canonicalize_observed_link
from threadrelay.contracts import validate_contract


class ExtractionIssue(TypedDict):
    code: str
    detail: str


class QuoteAttribution(TypedDict):
    author: str
    text: str
    permalink: str | None


class ObservedPost(TypedDict):
    post_id: str
    permalink: str
    author: str
    text: str
    content_hash: str
    visibility: str
    quality_flags: list[ExtractionIssue]
    observed_at: str
    source_session_id: str
    quote: QuoteAttribution | None
    observed_metadata: NotRequired[dict[str, Any]]


PUBLIC_CARD_SELECTOR = 'div[data-pressable-container="true"][data-interactive-id=""]'
CARD_SELECTOR = "main [role=feed] > article, " + PUBLIC_CARD_SELECTOR
# Post reads are scoped to one recognized rendered card. The only page-level
# evidence is visible login/dialog state; no whole-card/page text fallback. The
# snapshot runs in one DOM turn before later scrolling can virtualize it away.
RENDERED_SNAPSHOT_JS = r"""(card) => {
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
    if (attention) return issue('unknown_layout');
    if (!card || !displayed(card)) return issue('not_visible');
    if (!card.matches('article') && !card.matches(publicSelector)) return issue('unknown_layout');
    const rect=card.getBoundingClientRect();
    if (rect.bottom<=0 || rect.top>=innerHeight || rect.right<=0 || rect.left>=innerWidth) return issue('not_visible');
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
        if(headers.length!==1 || bodies.length!==1) return issue('unknown_layout');
        const header=headers[0], body=bodies[0];
        // header's nearest article/quote is the same root, not the header.
        const headerOwn = (selector,textual=false) => [...header.querySelectorAll(selector)]
            .filter(el=>el.closest('article,blockquote')===root && (textual?headerTextSupported(el):visible(el) && !unsupportedTextLayout(el)));
        const as=headerOwn('a[rel~="author"]',true);
        if(as.length!==1) return issue('unknown_layout');
        const author=text(as[0],true);
        const links=headerOwn('a[href]',true).filter(a=>[...a.querySelectorAll('time')].some(headerTextSupported));
        if(links.length!==1) return issue('missing_permalink');
        if(clipped(body)) return issue('text_clipped');
        if(unsupportedTextLayout(body)) return issue('unsupported_text_layout');
        const bodyRect=body.getBoundingClientRect();
        if(bodyRect.top<0 || bodyRect.bottom>innerHeight || bodyRect.left<0 || bodyRect.right>innerWidth) return issue('partially_visible');
        const bodyText=text(body);
        if(author===null || bodyText===null) return issue('text_too_large');
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
        if (card.parentElement.closest(boundary)) return issue('unknown_layout');
        const columns=card.firstElementChild;
        if (!columns || columns.tagName!=='DIV' || columns.children.length!==3 ||
            ![...columns.children].every(el=>el.tagName==='DIV')) return issue('unknown_layout');
        const [avatar,header,content]=columns.children;
        if (!displayed(header) || !displayed(content)) return issue('not_visible');
        if ([...content.querySelectorAll(boundary)].some(displayed)) return issue('unknown_layout');
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
        if (authors.length!==1) return issue('unknown_layout');
        const authorLink=authors[0], profile=platformUrl(authorLink);
        const handle=profile.pathname.split('/')[1];
        const label=text(authorLink,true);
        if (label===null || label.replace(/^@/,'').toLowerCase()!==handle.slice(1).toLowerCase()) return issue('unknown_layout');
        const times=headerLinks.filter(a=>[...a.querySelectorAll('time')].filter(headerTextSupported).length===1);
        if (times.length!==1) return issue('missing_permalink');
        const permalink=platformUrl(times[0]);
        const match=permalink && permalink.pathname.match(new RegExp('^/(@[A-Za-z0-9_.]{1,30})/post/[A-Za-z0-9_-]{1,128}/?$'));
        if (!match || match[1].toLowerCase()!==handle.toLowerCase()) return issue('unknown_layout');
        // A second post identity inside content is an unsupported quote/repost,
        // even if the platform did not expose a semantic blockquote boundary.
        if ([...content.querySelectorAll('a[href]')].some(a=>{
            const u=platformUrl(a);
            return displayed(a) && u && new RegExp('^/@[A-Za-z0-9_.]{1,30}/post/').test(u.pathname);
        })) return issue('unknown_layout');
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
        if (bodies.length>1 || (!bodies.length&&!media)) return issue('unknown_layout');
        // A recognized media-only card has no caption to invent or read.
        const body=bodies[0]??null;
        if (body && clipped(body)) return issue('text_clipped');
        if (body && unsupportedTextLayout(body)) return issue('unsupported_text_layout');
        const bodyText=body?text(body):'';
        if(bodyText===null) return issue('text_too_large');
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
}"""


def captured_snapshot_fingerprint(post: ObservedPost) -> str:
    """Deduplicate validated captured data independently from model identity.

    Keep metadata revisions, but exclude the observation envelope so timestamps
    alone never emit a new post each poll. ArchiveStore retains the full emitted
    observation; content_hash/classification cache policy stay unchanged.
    """
    snapshot = {key: value for key, value in post.items()
                if key not in {"observed_at", "source_session_id"}}
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def issue(code: str) -> ExtractionIssue:
    # Fixed diagnostics only; never leak unsupported card/page content into logs.
    result: ExtractionIssue = {"code": code, "detail": code.replace("_", " ")}
    validate_contract("ExtractionIssue", result)
    return result


def _valid_author(raw: dict[str, Any], identity: dict[str, str]) -> bool:
    try:
        author_url = urlsplit(raw["author_url"])
        handle = urlsplit(identity["permalink"]).path.split("/")[1]
        return (author_url.scheme == "https" and author_url.hostname in HOSTS
                and author_url.username is None and author_url.password is None
                and author_url.port is None and not author_url.query and not author_url.fragment
                and author_url.path.rstrip("/").lower() == f"/{handle}"
                and raw["author"].lower() == handle and len(raw["author"]) <= 500)
    except (KeyError, ValueError, TypeError):
        return False


def validate_observed_metadata(value: Any) -> dict[str, Any]:
    """Strict bounded DTO plus query-free public references, without fetching."""
    validate_contract("ObservedMetadata", value)
    for media in value.get("media", []):
        if "url" not in media:
            continue
        url = urlsplit(media["url"])
        host = (url.hostname or "").rstrip(".")
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or url.port is not None or url.query or url.fragment or "." not in host
                or any(ord(character) <= 32 for character in media["url"])
                or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test", ".home.arpa"))):
            raise ValueError("invalid_observed_media_reference")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("invalid_observed_media_reference")
    return deepcopy(value)


def post_from_snapshot(raw: dict[str, Any], *, source_session_id: str, observed_at: datetime) -> ObservedPost | ExtractionIssue:
    if raw.get("issue"):
        return issue(raw["issue"])
    identity = canonicalize_observed_link(raw.get("permalink", ""))
    if identity is None:
        return issue("missing_permalink")
    if not _valid_author(raw, identity):
        return issue("author_mismatch")
    try:
        metadata = validate_observed_metadata(raw["observed_metadata"]) if "observed_metadata" in raw else None
    except (ValueError, TypeError):
        return issue("unknown_layout")
    flags: list[ExtractionIssue] = []
    audience = raw["audience"]
    # The fixed rendered adapter distinguishes signed-out public access from
    # an audience badge. Contradictory or untyped evidence never upgrades visibility.
    guest_context = raw.get("guest_public_context", False)
    if (type(guest_context) is not bool or not isinstance(audience, list)
            or any(not isinstance(value, str) for value in audience)
            or (guest_context and audience not in ([], ["Public"]))):
        return issue("unknown_layout")
    visibility = {"Public": "public", "Private profile": "restricted", "Followers only": "restricted"}.get(audience[0], "unknown") if len(audience) == 1 else "unknown"
    if guest_context and not audience:
        visibility = "public"
    if visibility == "unknown":
        flags.append(issue("visibility_unverified"))
    if raw["truncated"]:
        flags.append(issue("truncated"))
    if raw.get("missing_media_context", False):
        flags.append(issue("missing_media_context"))
    if raw.get("quote_missing_media_context", False):
        flags.append(issue("quote_missing_media_context"))
    quote = None
    quoted = raw["quote"]
    if raw["quote_count"]:
        if raw["quote_count"] != 1 or quoted is None or quoted.get("issue"):
            flags.append(issue("quote_omitted"))
        else:
            quote_identity = canonicalize_observed_link(quoted["permalink"])
            if quote_identity is None or not _valid_author(quoted, quote_identity):
                flags.append(issue("quote_omitted"))
            else:
                quote = {"author": quoted["author"], "text": quoted["text"], "permalink": quote_identity["permalink"]}
                if quoted["truncated"]:
                    flags.append(issue("quote_truncated"))
                if raw["nested_quote"]:
                    flags.append(issue("nested_quote_omitted"))
    snapshot = {"author": raw["author"], "text": raw["text"], "quote": quote, "visibility": visibility, "quality_flags": flags}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if observed_at.tzinfo is None or observed_at.utcoffset() != timezone.utc.utcoffset(observed_at):
        raise ValueError("observed_at must be timezone-aware UTC")
    result: ObservedPost = {**identity, **snapshot, "content_hash": digest, "observed_at": observed_at.isoformat(), "source_session_id": source_session_id}
    # Model content identity stays stable when counters/time/media references
    # change. The archive separately binds the complete captured metadata.
    if metadata is not None:
        result["observed_metadata"] = metadata
    validate_contract("ObservedPost", result)
    return result


def extract_rendered_post(card: Locator, *, source_session_id: str | None = None, observed_at: datetime | None = None) -> ObservedPost | ExtractionIssue:
    """Read one recognized card atomically; no URL navigation or controls."""
    if not isinstance(source_session_id, str) or not 1 <= len(source_session_id) <= 128:
        return issue("source_session_required")
    raw = card.evaluate(RENDERED_SNAPSHOT_JS)
    return post_from_snapshot(raw, source_session_id=source_session_id, observed_at=observed_at or datetime.now(timezone.utc))
