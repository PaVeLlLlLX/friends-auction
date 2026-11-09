'use strict';
const root = document.getElementById('app');
const toastEl = document.getElementById('toast');
const state = {me:null, lots:[], demo:false, trial:false, role:sessionStorage.getItem('demoRole') || 'guest', end:0, adminView:null, adminLotId:null, applications:null, adminLotSignature:null, myBidsSignature:null};
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => `${Number(n).toLocaleString('ru-RU')} ₽`;
const decl = n => n%10===1 && n%100!==11 ? 'вещь' : n%10>=2 && n%10<=4 && (n%100<12 || n%100>14) ? 'вещи' : 'вещей';
const bidsWord = n => n%10===1 && n%100!==11 ? 'ставка' : n%10>=2 && n%10<=4 && (n%100<12 || n%100>14) ? 'ставки' : 'ставок';
let toastTimer;
let pendingWrites=0, mutationVersion=0, loading=0, viewVersion=0, ready=false, detailRenderedSignature=null;
function toast(message) { toastEl.textContent=message; toastEl.classList.add('visible'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>toastEl.classList.remove('visible'),4000); }
async function api(path, options={}) {
  const isWrite=options.method && options.method.toUpperCase()!=='GET';
  if(isWrite){pendingWrites++;mutationVersion++;}
  try {
  const headers = {...(options.headers || {})};
  if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;
  if (state.demo) headers['X-Demo-User'] = state.role;
  if (options.body && !(options.body instanceof FormData)) { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body); }
  let response; try { response=await fetch(path,{...options,headers}); }
  catch { throw new Error('Не удалось связаться с приложением. Проверьте соединение.'); }
  let data; try { data=await response.json(); } catch { throw new Error('Не удалось связаться с приложением.'); }
  if(!response.ok) throw new Error(data.error || 'Что-то пошло не так.');
  return data;
  } finally {if(isWrite)pendingWrites--;}
}
const post=(path,body={})=>api(path,{method:'POST',body});
// Enter in a single-line field finishes editing; only an explicit button submits.
document.addEventListener('keydown', event=>{
  const target=event.target;
  if(event.key==='Enter' && !event.isComposing && target instanceof HTMLInputElement && ['text','number','email','tel','url','search'].includes(target.type)){
    event.preventDefault();
    target.blur();
  }
});
const authHint=()=>'<div class="panel"><h3>Откройте в Telegram</h3><p>Для ставок и подписок откройте это приложение через бота в Telegram. Каталог доступен всем.</p></div>';
function nav(which){document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('active',a.dataset.nav===which));}
function demoBar(){
  const old=document.querySelector('.demo-strip'); if(old) old.remove();
  if (!state.demo) return;
  const el=document.createElement('div');el.className='demo-strip';el.textContent='Локальный деморежим · вымышленные данные';
  const s=document.createElement('select');s.setAttribute('aria-label','Роль в деморежиме');
  for(const [v,t] of [['guest','Гость'],['friend','Участник Аня'],['other','Участник Миша'],['admin','Организатор']]) {const o=document.createElement('option');o.value=v;o.textContent=t;s.append(o);}
  s.value=state.role;s.onchange=()=>{state.role=s.value;sessionStorage.setItem('demoRole',s.value);location.hash='#/';load()};el.append(s);document.body.prepend(el);
}
function trialBar(){
  const old=document.querySelector('.trial-strip'); if(old) old.remove();
  if(!state.trial || state.demo) return;
  const el=document.createElement('div');el.className='trial-strip';el.textContent='Тестовая версия · ставки не являются настоящими';document.body.prepend(el);
}
async function load(){
 loading++;
 try {
   const me=await api('/api/me'); state.demo=me.demo; state.trial=me.trial;
   if(state.demo) {const again=await api('/api/me');state.me=again.user;} else state.me=me.user;
   state.end=me.end; document.getElementById('admin-link').hidden=!(state.me && state.me.admin);demoBar();trialBar();
   const data=await api('/api/lots');state.lots=data.lots;ready=true;render();
 } catch(e) { root.innerHTML='<div class="page"><div class="empty"><strong>Не получилось загрузить аукцион</strong><p>'+esc(e.message)+'</p></div></div>'; }
 finally{loading--;}
}
function card(lot){
 const image=lot.photos.length?`<img src="${esc(lot.photos[0])}" alt="">`:'<span class="placeholder-art">Нет фото</span>';
 const status=lot.status==='cancelled'?'Снят':lot.status==='ended'?'Завершён':lot.condition;
 return `<a class="card" href="#/lot/${lot.id}"><div class="card-image">${image}${status?`<span class="photo-label">${esc(status)}</span>`:''}</div><div class="card-bottom"><h3 class="card-title">${esc(lot.title)}</h3><div class="card-price-row"><div><span class="small-label">${lot.price===null?'Начальная цена':'Текущая ставка'}</span><span class="price">${money(lot.price===null?lot.start_price:lot.price)}</span></div><span class="card-arrow" aria-hidden="true">↗</span></div><p class="card-meta">${lot.count} ${bidsWord(lot.count)} <span aria-hidden="true">·</span> шаг ${money(lot.step)}</p></div></a>`;
}
function catalog(){
 nav('catalog'); const visible=state.lots;
 root.innerHTML=`<section class="catalog-heading"><div><p class="catalog-kicker">Каталог <span>/</span> ${visible.length} ${decl(visible.length)}</p><h1>Все лоты</h1><p>Выберите вещь, чтобы посмотреть описание и сделать ставку.</p></div><a class="rules-link" href="#/about">Правила участия <span aria-hidden="true">↗</span></a></section>
 <section class="auction-info" aria-label="Условия аукциона"><div><span class="info-label">Окончание торгов</span><strong>12 декабря, 23:59 <small>UTC+7</small></strong></div><div><span class="info-label">Получение</span><strong>Самовывоз</strong></div><span class="auction-info-note">Без продления</span></section>
 <div class="section-head"><h2>Вещи</h2><span class="count">${visible.length} ${decl(visible.length)}</span></div>
 ${visible.length?`<div class="grid">${visible.map(card).join('')}</div>`:'<div class="empty"><strong>Пока нет лотов</strong><p>Здесь появятся опубликованные вещи.</p></div>'}<footer class="footnote"><span>Аукцион · 2026</span><a href="#/about">Условия участия</a></footer>`;
}
function detail(lot){
 detailRenderedSignature=JSON.stringify([lot,state.me && state.me.status,Boolean(state.me && state.me.admin)]);
 nav('catalog'); const price=lot.price===null?lot.start_price:lot.price; const minimum=lot.price===null?lot.start_price:lot.price+lot.step;
 const images=lot.photos.length?`<img id="main-photo" src="${esc(lot.photos[0])}" alt="${esc(lot.title)}">`:'<span class="placeholder-art">Нет фото</span>';
 const thumbs=lot.photos.length>1?`<div class="thumbs">${lot.photos.map((p,i)=>`<button data-photo="${esc(p)}" aria-label="Фотография ${i+1}"><img src="${esc(p)}" alt=""></button>`).join('')}</div>`:'';
 let action='';
 if(lot.status!=='active') action=`<div class="panel"><strong>${lot.status==='cancelled'?'Лот снят с торгов':lot.leading?'Вы победили в торгах':'Торги завершены'}</strong>${lot.status==='ended'&&lot.leading?'<p>Я свяжусь с вами, чтобы договориться о передаче.</p>':''}</div>`;
 else if(state.me && state.me.admin && state.me.status!=='approved') action='';
 else if(!state.me) action=authHint();
 else if(state.me.status==='pending') action='<div class="panel"><h3>Заявка на рассмотрении</h3><p>Я гляну, кто вы, и одобрю заявку. После этого вы сможете сделать ставку.</p></div>';
 else if(state.me.status==='rejected') action='<div class="panel"><h3>Доступ к ставкам не одобрен</h3><p>Если вы со мной знакомы, напишите мне лично.</p></div>';
 else if(state.me.status!=='approved') action='<div class="panel application-panel"><h3>Заявка на участие</h3><p>Укажите имя и фамилию — я гляну, кто вы, и одобрю заявку. Эти данные вижу только я.</p><form id="application-form"><label class="field"><span>Имя</span><input name="given_name" autocomplete="given-name" enterkeyhint="done" maxlength="100" required></label><label class="field"><span>Фамилия</span><input name="family_name" autocomplete="family-name" enterkeyhint="done" maxlength="100" required></label><p class="hint">После одобрения можно делать ставки на все лоты.</p><button class="button full" type="submit">Отправить заявку</button></form></div>';
 else if(lot.leading) action='<div class="panel"><h3>Ваша ставка сейчас лидирует</h3><p>Настройки уведомлений доступны ниже.</p></div>';
 else action=`<div class="field"><span>Ваша ставка, ₽</span><div class="input-row"><input id="bid-amount" type="number" inputmode="numeric" min="${minimum}" step="${lot.step}" value="${minimum}"><button class="button" id="bid">Сделать ставку ↗</button></div><p class="hint" id="live-bid-hint">Минимум ${money(minimum)} · шаг ${money(lot.step)}. Можно повысить сразу на несколько шагов.</p></div>`;
 const subs=state.me?`<div class="toggle-row"><span>Следить за новыми ставками</span><input type="checkbox" id="watch" ${lot.watching?'checked':''} aria-label="Следить за новыми ставками"></div>${lot.has_bid?`<div class="toggle-row"><span>Уведомления об участии</span><input type="checkbox" id="participation" ${lot.participation?'checked':''} aria-label="Уведомления об участии"></div>`:''}<p class="hint">Уведомления приходят от бота. Начните с ним диалог, чтобы их получать.</p>`:'';
 const history=state.me && state.me.admin?`<section class="admin-bid-history" aria-labelledby="admin-history-title"><div class="admin-history-heading"><h2 id="admin-history-title">История ставок</h2><span>Видно только вам</span></div><div id="admin-bid-history" data-lot="${lot.id}" aria-live="polite"><p class="hint">Загружаю ставки…</p></div></section>`:'';
 root.innerHTML=`<div class="page"><a class="back" href="#/">← Все лоты</a><div class="detail-layout"><div><div class="detail-photo">${images}</div>${thumbs}</div><div class="detail"><p class="eyebrow">Самовывоз</p><h1 id="live-title">${esc(lot.title)}</h1><p class="detail-description" id="live-description">${esc(lot.description || 'Описание не указано.')}</p><p class="hint" id="live-condition">Состояние: ${esc(lot.condition || 'не указано')}</p><div class="detail-info"><div><span class="small-label" id="live-price-label">${lot.price===null?'Начальная цена':'Текущая ставка'}</span><strong id="live-price">${money(price)}</strong></div><div><span class="small-label">Завершение торгов</span><strong>12 декабря</strong><div class="hint">23:59 · UTC+7</div></div></div><p class="live-change" id="live-change" role="status" hidden></p>${action}${subs}${history}</div></div></div>`;
 document.querySelectorAll('[data-photo]').forEach(el=>el.onclick=()=>{document.getElementById('main-photo').src=el.dataset.photo});
 const application=document.getElementById('application-form'); if(application) application.onsubmit=async event=>{event.preventDefault();const submit=application.querySelector('button');const form=new FormData(application);submit.disabled=true;try{await post('/api/application',{given_name:form.get('given_name'),family_name:form.get('family_name')});toast('Заявка отправлена');await load()}catch(e){submit.disabled=false;toast(e.message)}};
 const bid=document.getElementById('bid'); if(bid) bid.onclick=async()=>{const entered=document.getElementById('bid-amount').value.trim(),amount=Number(entered);const current=state.lots.find(item=>item.id===lot.id);if(!current || current.status!=='active' || !state.me || state.me.status!=='approved' || current.leading){toast('Условия лота изменились. Проверьте актуальный статус.');return}if(!entered || !Number.isSafeInteger(amount)){toast('Введите сумму в целых рублях.');return} if(!confirm(`Подтвердить ставку ${money(amount)}?`))return;bid.disabled=true;try{await post(`/api/lots/${lot.id}/bid`,{amount,expected_price:current.price});toast('Ставка принята');await load()}catch(e){toast(e.message);bid.disabled=false;requestImmediateSync()}};
 for(const kind of ['watch','participation']) {const el=document.getElementById(kind);if(el)el.onchange=async()=>{try{await post(`/api/lots/${lot.id}/subscription`,{kind:kind==='watch'?'watching':'participation',enabled:el.checked});toast('Настройки сохранены')}catch(e){el.checked=!el.checked;toast(e.message)}};}
 if(state.me && state.me.admin)loadProductHistory(lot.id);
}
function drawProductHistory(lotId,data){
 const box=document.getElementById('admin-bid-history');
 if(!box || box.dataset.lot!==String(lotId) || !state.me || !state.me.admin)return;
 const signature=JSON.stringify(data.bids);
 if(box.historySignature===signature)return;
 box.historySignature=signature;
 box.innerHTML=data.bids.length?`<div class="admin-history-list">${data.bids.map(b=>`<div class="admin-history-row"><div class="admin-history-person"><strong>${privateName(b)}</strong><small>${b.username?'@'+esc(b.username)+' · ':''}${esc(new Date(b.created*1000).toLocaleString('ru-RU',{timeZone:'Asia/Novosibirsk',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}))}</small></div><strong class="admin-history-amount">${money(b.amount)}</strong></div>`).join('')}</div>`:'<p class="hint">Ставок пока нет.</p>';
}
async function loadProductHistory(lotId){
 const box=document.getElementById('admin-bid-history'),role=state.role;
 try{const data=await api(`/api/admin/lots/${lotId}`);if(document.getElementById('admin-bid-history')===box && state.role===role && box && !box.historySignature)drawProductHistory(lotId,data)}
 catch(e){if(box && !box.historySignature && document.getElementById('admin-bid-history')===box && state.me && state.me.admin)box.innerHTML='<p class="hint">Не удалось загрузить историю. Попробую ещё раз.</p>';}
}
function drawMyBids(data){
 const box=document.getElementById('my-content');if(!box)return;
 state.myBidsSignature=JSON.stringify([data.bids,state.lots]);
 if(!data.bids.length){box.innerHTML='<div class="empty"><strong>Пока здесь пусто</strong><p>Ваши ставки появятся здесь после участия в торгах.</p><a class="button" href="#/">Посмотреть вещи ↗</a></div>';return}
 box.innerHTML=data.bids.map(b=>{const lot=state.lots.find(x=>x.id===b.lot_id);const status=lot && lot.status==='cancelled'?'Лот снят':lot && lot.status==='ended'?(lot.leading?'Вы победили':'Торги завершены'):lot && lot.leading?'Вы лидируете':'Ставку перебили';return `<a class="list-item" href="#/lot/${b.lot_id}"><span><strong>${esc(b.title)}</strong><small>${new Date(b.created*1000).toLocaleString('ru-RU',{timeZone:'Asia/Novosibirsk'})} · ${status}</small></span><strong>${money(b.amount)}</strong></a>`}).join('');
}
async function my(){
 nav('my');root.innerHTML='<div class="page"><p class="eyebrow">Всё важное в одном месте</p><h1 class="page-title">Мои ставки</h1><div id="my-content"></div></div>';
 const box=document.getElementById('my-content'); if(!state.me){box.innerHTML=authHint();return}
 try{const data=await api('/api/my-bids');if(location.hash==='#/my' && document.getElementById('my-content')===box)drawMyBids(data)}catch(e){if(document.getElementById('my-content')===box)box.textContent=e.message}
}
function about(){nav('about');root.innerHTML=`<div class="page"><p class="eyebrow">Информация</p><h1 class="page-title">Как участвовать</h1><div class="panel explain"><h3>01 · Выберите лот</h3><p>Каталог открыт всем. Получение вещей — только самовывозом.</p><h3>02 · Подайте заявку</h3><p>Перед первой ставкой укажите имя и фамилию. Я гляну, кто вы, и одобрю заявку. Имя, фамилию и данные Telegram вижу только я. После одобрения можно участвовать во всех лотах.</p><h3>03 · Сделайте ставку</h3><p>У каждого лота есть начальная цена и шаг. Можно поднять ставку на несколько шагов. Торги заканчиваются 12 декабря 2026 года в 23:59 (UTC+7), без продления.</p><h3>04 · Получите вещь</h3><p>Побеждает самая высокая ставка. Об оплате и получении договоримся лично.</p><h3>Уведомления и данные</h3><p>Уведомления приходят только по лотам, в которых вы участвуете или за которыми следите. Их можно отключить. Ставки других участников и их имена скрыты. Данные аукциона хранятся до 31 января 2027 года включительно.</p></div></div>`}
function privateName(user){return esc([user.applicant_given_name,user.applicant_family_name].filter(Boolean).join(' ') || user.first_name || 'Имя не указано');}
const applicationSignature=users=>JSON.stringify([...users].sort((a,b)=>a.id-b.id));
function adminShell(tab){nav('');root.innerHTML=`<div class="page"><div class="admin-header"><div><p class="eyebrow">Для организатора</p><h1 class="page-title">Управление</h1></div><a class="back" href="#/">← На главную</a></div><div class="tabs"><button class="tab ${tab==='lots'?'active':''}" data-tab="lots">Лоты</button><button class="tab ${tab==='applications'?'active':''}" data-tab="applications">Заявки</button><button class="tab ${tab==='new'?'active':''}" data-tab="new">+ Добавить</button></div><div id="admin-content"></div></div>`;document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>admin(b.dataset.tab));}
function drawApplications(data){
 const box=document.getElementById('admin-content');if(!box)return;
 state.applications=data.users;
 box.innerHTML=data.users.length?data.users.map(u=>`<div class="list-item application-item" data-application="${u.id}" data-current-status="${esc(u.status)}"><div class="application-person"><strong>${privateName(u)} ${u.username?'@'+esc(u.username):''}</strong><small>Telegram ID: ${u.id}</small><span class="application-status ${u.status==='approved'?'approved':u.status==='rejected'?'rejected':'pending'}" role="status">${u.status==='pending'?'Ожидает решения':u.status==='approved'?'Допущен к ставкам':'Доступ отклонён'}</span><p class="application-result" aria-live="polite" hidden></p></div><div class="actions"><button type="button" class="button secondary" data-user="${u.id}" data-status="approved" ${u.status==='approved'?'disabled':''}>Допустить</button><button type="button" class="button danger" data-user="${u.id}" data-status="rejected" ${u.status==='rejected'?'disabled':''}>Отказать</button></div></div>`).join(''):'<div class="empty"><strong>Заявок пока нет</strong></div>';
 box.querySelectorAll('[data-user]').forEach(b=>b.onclick=async()=>{
   const row=b.closest('[data-application]');const buttons=Array.from(row.querySelectorAll('[data-user]'));const message=row.querySelector('.application-result');const badge=row.querySelector('.application-status');const original=b.textContent;
   buttons.forEach(button=>{button.disabled=true});b.textContent='Сохраняю…';message.hidden=true;
   try{
     const status=b.dataset.status;await post(`/api/admin/users/${b.dataset.user}`,{status});row.dataset.currentStatus=status;
     const applicant=state.applications && state.applications.find(user=>String(user.id)===b.dataset.user);if(applicant)applicant.status=status;
     badge.className=`application-status ${status}`;badge.textContent=status==='approved'?'Допущен к ставкам':'Доступ отклонён';
     message.textContent=status==='approved'?'Решение сохранено. Участник может делать ставки.':'Решение сохранено. Доступ к ставкам закрыт.';message.className='application-result success';message.hidden=false;
   }catch(e){message.textContent=`Не удалось сохранить решение: ${e.message}`;message.className='application-result error';message.hidden=false;}
   finally{b.textContent=original;buttons.forEach(button=>{button.disabled=button.dataset.status===row.dataset.currentStatus})}
 });
}
async function admin(tab='lots'){
 if(!state.me || !state.me.admin){root.innerHTML='<div class="page"><div class="empty"><strong>Нет доступа</strong></div></div>';return} viewVersion++;state.adminView=tab;state.adminLotId=null;adminShell(tab);const box=document.getElementById('admin-content');
 if(tab==='new'){
 box.innerHTML=`<div class="panel"><h3>Новая вещь</h3><p>Торги по всем лотам заканчиваются 12 декабря в 23:59.</p><form id="new-lot"><label class="field"><span>Название <span class="required">*</span></span><input name="title" maxlength="120" required placeholder="Например, набор бокалов"></label><label class="field"><span>Описание</span><textarea name="description" maxlength="3000" placeholder="Расскажите немного о вещи"></textarea></label><label class="field"><span>Состояние</span><input name="condition" maxlength="120" placeholder="Например, почти новое"></label><div class="input-row"><label class="field"><span>Начальная цена, ₽</span><input name="start_price" type="number" min="0" max="100000000" value="0" required></label><label class="field"><span>Шаг ставки, ₽</span><input name="step" type="number" min="1" max="100000000" value="50" required></label></div><label class="field"><span>Фотографии · до 6</span><input name="photos" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" multiple></label><p class="hint">Фотографии станут публичными. Не добавляйте изображения с адресом или другой личной информацией.</p><button class="button full" type="submit">Опубликовать вещь ↗</button></form></div>`;
 document.getElementById('new-lot').onsubmit=async e=>{e.preventDefault();const btn=e.target.querySelector('button');btn.disabled=true;try{await api('/api/admin/lots',{method:'POST',body:new FormData(e.target)});toast('Вещь опубликована');const d=await api('/api/lots');state.lots=d.lots;admin('lots')}catch(err){toast(err.message);btn.disabled=false}};return;}
 if(tab==='applications'){
 try{const data=await api('/api/admin/applications');if(state.adminView==='applications' && document.getElementById('admin-content')===box)drawApplications(data)}catch(e){if(document.getElementById('admin-content')===box)box.textContent=e.message}return;}
 box.innerHTML=state.lots.length?state.lots.map(l=>`<div class="list-item"><div><strong>${esc(l.title)}</strong><small>${l.status==='cancelled'?'Снят':l.status==='ended'?'Завершён':'На торгах'} · ${l.count} ставок · ${money(l.price===null?l.start_price:l.price)}</small></div><button class="button secondary" data-manage="${l.id}">Открыть</button></div>`).join(''):'<div class="empty"><strong>Вещей пока нет</strong><p>Добавьте первый лот с помощью кнопки выше.</p></div>';
 box.querySelectorAll('[data-manage]').forEach(b=>b.onclick=()=>adminLot(Number(b.dataset.manage)));
}
async function adminLot(id, preloaded=null){
 viewVersion++;state.adminView='lot';state.adminLotId=id;
 try{const data=preloaded || await api(`/api/admin/lots/${id}`);if(location.hash!=='#/admin' || state.adminView!=='lot' || state.adminLotId!==id)return;state.adminLotSignature=JSON.stringify(data);const l=data.lot;const box=document.getElementById('admin-content');if(!box)return;box.innerHTML=`<button class="back" id="admin-back" style="border:0;background:none;padding:0">← Все лоты</button><div class="panel"><h3>${esc(l.title)}</h3><p>${l.status==='active'?'Идут торги':l.status==='ended'?'Торги завершены':'Лот снят'} · ${l.count} ставок</p><div class="actions">${l.status==='active'&&l.count===0?'<button class="button secondary" id="edit-lot">Редактировать</button>':''}${l.status==='active'?'<button class="button danger" id="cancel-lot">Снять лот</button>':''}${l.status==='ended'&&data.candidates.length?`<button class="button danger" id="refuse">Текущий участник отказался</button>${data.has_refusals?'<button class="button secondary" id="notify-candidate">Уведомить следующего</button>':''}<button class="button secondary" data-fulfill="agreed">Договорились</button><button class="button secondary" data-fulfill="delivered">Передано</button>`:''}</div><p>Передача: ${data.fulfillment==='agreed'?'договорились':data.fulfillment==='delivered'?'передано':'не отмечено'}</p></div><h3>Участники по ставке</h3>${data.candidates.length?data.candidates.map((u,i)=>`<div class="list-item"><span><strong>${i===0?'★ ':''}${privateName(u)} ${u.username?'@'+esc(u.username):''}</strong><small>Telegram ID: ${u.id}</small></span><strong>${money(u.amount)}</strong></div>`).join(''):'<p class="hint">Пока нет ставок.</p>'}<h3>История ставок</h3>${data.bids.map(b=>`<div class="list-item"><span>${privateName(b)} ${b.username?'@'+esc(b.username):''}</span><strong>${money(b.amount)}</strong></div>`).join('')}`;
 document.getElementById('admin-back').onclick=()=>admin('lots');
 const edit=document.getElementById('edit-lot');if(edit)edit.onclick=()=>{viewVersion++;state.adminView='edit';box.innerHTML=`<button class="back" id="admin-back" style="border:0;background:none;padding:0">← К лоту</button><div class="panel"><h3>Изменить вещь</h3><p>Редактировать можно до первой ставки. Для замены фотографий пока нужно снять лот и создать заново.</p><p class="live-change" id="live-edit-warning" role="status" hidden></p><form id="edit-form"><label class="field"><span>Название</span><input name="title" maxlength="120" required value="${esc(l.title)}"></label><label class="field"><span>Описание</span><textarea name="description" maxlength="3000">${esc(l.description)}</textarea></label><label class="field"><span>Состояние</span><input name="condition" maxlength="120" value="${esc(l.condition)}"></label><label class="field"><span>Начальная цена, ₽</span><input name="start_price" type="number" min="0" required value="${l.start_price}"></label><label class="field"><span>Шаг ставки, ₽</span><input name="step" type="number" min="1" required value="${l.step}"></label><button class="button full">Сохранить</button></form></div>`;document.getElementById('admin-back').onclick=()=>adminLot(id);document.getElementById('edit-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{await post(`/api/admin/lots/${id}/edit`,{title:f.get('title'),description:f.get('description'),condition:f.get('condition'),start_price:Number(f.get('start_price')),step:Number(f.get('step'))});const x=await api('/api/lots');state.lots=x.lots;toast('Изменения сохранены');adminLot(id)}catch(err){toast(err.message)}}};
 const cancel=document.getElementById('cancel-lot');if(cancel)cancel.onclick=async()=>{if(confirm('Снять лот с торгов?')){try{await post(`/api/admin/lots/${id}`,{action:'cancel'});toast('Лот снят');const x=await api('/api/lots');state.lots=x.lots;admin('lots')}catch(e){toast(e.message)}}};
 const refuse=document.getElementById('refuse');if(refuse)refuse.onclick=async()=>{if(confirm('Отметить отказ текущего участника?')){try{await post(`/api/admin/lots/${id}`,{action:'refuse'});adminLot(id)}catch(e){toast(e.message)}}};
 const notify=document.getElementById('notify-candidate');if(notify)notify.onclick=async()=>{if(confirm('Отправить сообщение следующему участнику?')){try{await post(`/api/admin/lots/${id}/notify-candidate`);toast('Сообщение отправлено')}catch(e){toast(e.message)}}};
 box.querySelectorAll('[data-fulfill]').forEach(b=>b.onclick=async()=>{try{await post(`/api/admin/lots/${id}`,{action:b.dataset.fulfill});adminLot(id)}catch(e){toast(e.message)}});
 }catch(e){toast(e.message)}
}
function render(){viewVersion++;const hash=location.hash || '#/';if(hash.startsWith('#/lot/')){const id=Number(hash.split('/')[2]);const l=state.lots.find(x=>x.id===id);if(l)detail(l);else root.innerHTML='<div class="page"><div class="empty"><strong>Лот не найден</strong></div></div>'}else if(hash==='#/my')my();else if(hash==='#/about')about();else if(hash==='#/admin')admin();else catalog();window.scrollTo(0,0);}

// The temporary tunnel does not support server-sent events. Poll while the app is
// visible, discard stale responses, and never replace a form containing a draft.
let syncTimer=null, syncing=false, resyncRequested=false;
function liveIndicator(connected){
 const indicator=document.getElementById('live-indicator');if(!indicator)return;
 const text=connected?'Автообновление':'Нет связи · повторяем';
 if(indicator.textContent!==text)indicator.textContent=text;
 indicator.classList.toggle('offline',!connected);
}
function detailHasDraft(){
 const active=document.activeElement;
 if(active && root.contains(active) && active.matches('input:not([type=checkbox]), textarea'))return true;
 const application=document.getElementById('application-form');
 if(application && Array.from(application.querySelectorAll('input')).some(input=>input.value.trim()!==''))return true;
 const amount=document.getElementById('bid-amount');
 return !!(amount && amount.value!==amount.defaultValue);
}
function patchDetail(lot){
 const byId=id=>document.getElementById(id);
 const title=byId('live-title'), description=byId('live-description'), condition=byId('live-condition');
 const price=byId('live-price'), label=byId('live-price-label'), hint=byId('live-bid-hint'), amount=byId('bid-amount');
 if(title)title.textContent=lot.title;
 if(description)description.textContent=lot.description || 'Описание не указано.';
 if(condition)condition.textContent='Состояние: '+(lot.condition || 'не указано');
 if(price)price.textContent=money(lot.price===null?lot.start_price:lot.price);
 if(label)label.textContent=lot.price===null?'Начальная цена':'Текущая ставка';
 const minimum=lot.price===null?lot.start_price:lot.price+lot.step;
 if(amount){amount.min=String(minimum);amount.step=String(lot.step)}
 if(hint)hint.textContent=`Минимум ${money(minimum)} · шаг ${money(lot.step)}. Можно повысить сразу на несколько шагов.`;
 const watch=byId('watch'), participation=byId('participation');
 if(watch)watch.checked=lot.watching;
 if(participation)participation.checked=lot.participation;
 const warning=byId('live-change'), bid=byId('bid');
 const previous=detailRenderedSignature ? JSON.parse(detailRenderedSignature)[0] : null;
 let message='';
 if(lot.status!=='active')message=lot.status==='cancelled'?'Лот снят с торгов.':'Торги завершены.';
 else if(bid && (!state.me || state.me.status!=='approved'))message='Доступ к ставкам изменился. Проверьте статус заявки.';
 else if(bid && lot.leading)message='Ваша ставка сейчас лидирует.';
 else if(previous && (previous.price!==lot.price || previous.step!==lot.step))message='Цена или шаг изменились. Проверьте сумму перед ставкой.';
 if(bid)bid.disabled=lot.status!=='active' || !state.me || state.me.status!=='approved' || lot.leading;
 if(warning){if(warning.textContent!==message)warning.textContent=message;warning.hidden=!message}
}
function requestImmediateSync(){
 if(syncing){resyncRequested=true;return}
 clearTimeout(syncTimer);syncTimer=setTimeout(syncNow,0);
}
async function syncNow(){
 if(syncing){resyncRequested=true;return}
 if(document.hidden || (tg && tg.isActive===false) || loading || pendingWrites){
   syncTimer=setTimeout(syncNow,3000);return;
 }
 if(!ready){await load();if(!ready)liveIndicator(false);syncTimer=setTimeout(syncNow,3000);return}
 syncing=true;const version=mutationVersion, currentView=viewVersion, route=location.hash || '#/', role=state.role;
 const adminView=state.adminView, adminLotId=state.adminLotId;
 const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),8000);
 try{
   const options={signal:controller.signal};
   const [me,lots]=await Promise.all([api('/api/me',options),api('/api/lots',options)]);
   let extra=null;
   if(route==='#/my' && me.user)extra=await api('/api/my-bids',options);
   if(route.startsWith('#/lot/') && me.user && me.user.admin)extra=await api(`/api/admin/lots/${Number(route.split('/')[2])}`,options);
   if(route==='#/admin' && me.user && me.user.admin && adminView==='applications')extra=await api('/api/admin/applications',options);
   if(route==='#/admin' && me.user && me.user.admin && adminView==='lot' && adminLotId!==null)extra=await api(`/api/admin/lots/${adminLotId}`,options);
   if(version!==mutationVersion || pendingWrites || loading || currentView!==viewVersion || route!==(location.hash || '#/') || role!==state.role)return;
   const oldMe=state.me, lotsChanged=JSON.stringify(state.lots)!==JSON.stringify(lots.lots);
   state.me=me.user;state.lots=lots.lots;state.end=lots.end;
   document.getElementById('admin-link').hidden=!(state.me && state.me.admin);
   liveIndicator(true);
   if(route==='#/' || route==='#'){
     if(lotsChanged)catalog();
   }else if(route.startsWith('#/lot/')){
     const id=Number(route.split('/')[2]), lot=state.lots.find(item=>item.id===id);
     const signature=lot?JSON.stringify([lot,state.me && state.me.status,Boolean(state.me && state.me.admin)]):null;
     if(signature!==detailRenderedSignature){
       if(lot && detailHasDraft())patchDetail(lot);
       else if(lot){const photo=document.getElementById('main-photo');const selected=photo && photo.getAttribute('src');detail(lot);const restored=document.getElementById('main-photo');if(restored && selected && lot.photos.includes(selected))restored.src=selected;}
       else root.innerHTML='<div class="page"><div class="empty"><strong>Лот не найден</strong></div></div>';
       if(oldMe && oldMe.status==='pending' && state.me && state.me.status==='approved')toast('Заявка одобрена. Теперь можно сделать ставку.');
     }
     if(lot && extra && state.me && state.me.admin)drawProductHistory(id,extra);
   }else if(route==='#/my'){
     if(!state.me && oldMe)my();
     else if(extra && JSON.stringify([extra.bids,state.lots])!==state.myBidsSignature)drawMyBids(extra);
   }else if(route==='#/admin'){
     if(!state.me || !state.me.admin)admin();
     else if(adminView==='applications' && extra && (!state.applications || applicationSignature(extra.users)!==applicationSignature(state.applications)))drawApplications(extra);
     else if(adminView==='lots' && lotsChanged)admin('lots');
     else if(adminView==='lot' && extra && JSON.stringify(extra)!==state.adminLotSignature)adminLot(adminLotId,extra);
     else if(adminView==='edit'){
       const lot=state.lots.find(item=>item.id===adminLotId), warning=document.getElementById('live-edit-warning'), save=document.querySelector('#edit-form button[type=submit], #edit-form button:not([type])');
       const unavailable=!lot || lot.status!=='active' || lot.count>0;
       if(warning){warning.hidden=!unavailable;warning.textContent=unavailable?'Условия изменились: редактирование этого лота больше недоступно. Ваш текст сохранён на экране.':''}
       if(save)save.disabled=unavailable;
     }
   }
 }catch(e){if(!document.hidden)liveIndicator(false)}
 finally{
   clearTimeout(timeout);syncing=false;
   const delay=resyncRequested?0:3000;resyncRequested=false;clearTimeout(syncTimer);syncTimer=setTimeout(syncNow,delay);
 }
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)requestImmediateSync()});
window.addEventListener('online',requestImmediateSync);
if(tg && typeof tg.onEvent==='function')tg.onEvent('activated',requestImmediateSync);
window.addEventListener('hashchange',render);document.getElementById('admin-link').onclick=()=>{location.hash='#/admin'};load().then(requestImmediateSync);
