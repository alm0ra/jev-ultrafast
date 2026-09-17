(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['INPUT','TEXTAREA'].includes(e.tagName) ?
        e.parentElement?.querySelector('legend')?.textContent?.trim() : '') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    // Frameworks can attach real click handlers to plain div/span controls.
    // Discover the observed DOM handler; never execute it during observation.
    if (typeof e.onclick==='function') return 'button';
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.scopeText=scope=>{
    if (!scope) return '';
    const copy=scope.cloneNode(true);
    // Other controls (e.g. a resend countdown) are not the selected action's context.
    copy.querySelectorAll('button,[role="button"],[role="timer"],script,style').forEach(n=>n.remove());
    return (copy.textContent||'').replace(/\s+/g,' ').trim().slice(0,6000);
  };
  cache.guard=(e,fieldOnly=false)=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),fieldOnly ? [identity(scope),scope?.getAttribute('action'),scope?.getAttribute('method')] : cache.scopeText(scope)];
  };
  cache.clickPoint=e=>{
    const r=e.getBoundingClientRect();
    for (const fy of [.5,.25,.75]) for (const fx of [.5,.25,.75,.1,.9]) {
      const x=r.x+r.width*fx,y=r.y+r.height*fy;
      if (x<0||y<0||x>=innerWidth||y>=innerHeight) continue;
      const top=document.elementFromPoint(x,y);
      if (!top || !e.contains(top)) continue;
      const owner=top.closest('a,button,input,select,textarea,[role="button"],[role="link"]');
      if (owner && owner!==e && e.contains(owner)) continue;
      return {x,y};
    }
    return null;
  };
  const actions=[];
  const suggestionsFor=field=>{
    const ids=((field.getAttribute('aria-controls')||'')+' '+(field.getAttribute('aria-owns')||'')).trim().split(/\s+/);
    let roots=ids.map(id=>document.getElementById(id)).filter(Boolean);
    if (!roots.length) {
      let parent=field.parentElement;
      for (let depth=0;parent && parent!==document.body && depth<4;depth++,parent=parent.parentElement) {
        if (parent.querySelector('[role="option"]')) {roots=[parent];break;}
      }
    }
    return roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')])
      .filter(e=>visible(e) && cache.clickPoint(e));
  };
  const candidates=new Set(document.querySelectorAll(selector));
  for (const e of document.querySelectorAll('*')) {
    if (e instanceof HTMLElement && typeof e.onclick==='function' &&
        getComputedStyle(e).cursor==='pointer' && !e.querySelector(selector) &&
        !e.parentElement?.closest(selector) && name(e).trim()) candidates.add(e);
  }
  for (const e of candidates) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (!cache.clickPoint(e)) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      if (editable) {
        const suggestions=suggestionsFor(e);
        base.suggestion_labels=suggestions.map(o=>(o.textContent||'').replace(/\s+/g,' ').trim());
        base.autocomplete = suggestions.length>0 || e.getAttribute('aria-autocomplete')==='list' ||
          e.getAttribute('aria-haspopup')==='listbox' || rname==='combobox';
      }
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={}, field_guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  for (const a of actions) if (a.kind==='fill') field_guards[a.node]=cache.guard(cache.nodes.get(a.node),true);
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  const focused=document.activeElement;
  const field=actions.find(a=>a.kind==='fill' && cache.nodes.get(a.node)===focused);
  const options=field ? suggestionsFor(focused) : [];
  const interaction=field && field.value.trim() && options.length ? {
    kind:'autocomplete', field_node:field.node, query:field.value,
    option_nodes:options.map(identity), selection_pending:true
  } : null;
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,field_guards,omitted_actions,interaction};
})()
