// CSRF helper
function getCookie(name){
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(';').shift();
  return '';
}
const __csrftoken = getCookie('csrftoken');

document.addEventListener('DOMContentLoaded', function() {
  // Marquee clock
  var el = document.getElementById('ribbonText');
  function pad(n) { return n < 10 ? '0' + n : n; }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  var dateStr = cap((new Date()).toLocaleDateString('fr-FR', {weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'}));
  function tick() { 
    var d = new Date(); 
    var timeStr = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); 
    if (el) { el.textContent = 'ECAGES • TABLEAU DE BORD COMMERCIAL • ' + dateStr + ' — ' + timeStr; }
  }
  tick();
  setInterval(tick, 1000);

  // Search
  const searchBox = document.getElementById('searchBox');
  const productRows = document.querySelectorAll('tbody tr[data-product-id]');
  function filterProducts() {
    const query = (searchBox?.value || '').toLowerCase().trim();
    if(!productRows.length) return;
    productRows.forEach(row => {
      const nameCell = row.querySelector('td:first-child');
      const brandCell = row.querySelector('td:nth-child(2)');
      const productName = (nameCell ? nameCell.textContent : '').toLowerCase();
      const brandName = (brandCell ? brandCell.textContent : '').toLowerCase();
      const hit = productName.includes(query) || brandName.includes(query);
      row.style.display = hit ? '' : 'none';
    });
  }
  async function fetchTable(q, type){
    try{
      const params = new URLSearchParams();
      if(q) params.set('q', q);
      if(type) params.set('type', type);
      const r = await fetch('/sales/commercial-dashboard/table/?'+params.toString(), {headers:{'X-Requested-With':'XMLHttpRequest'}, cache:'no-store'});
      const html = await r.text();
      const container = document.getElementById('productsTableContainer');
      if(!container) return;
      // Replace only the table content inside the container
      container.innerHTML = html;
    }catch(e){}
  }
  function debounce(fn, ms){ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); }; }
  const live = debounce(()=>{
    const q = (searchBox?.value||'').trim();
    const url = new URL(window.location.href);
    const type = url.searchParams.get('type')||'';
    fetchTable(q, type);
  }, 250);
  if (searchBox) {
    searchBox.addEventListener('input', live);
  }

  // Export Excel
  const btnExport = document.getElementById('btnExport');
  if (btnExport){
    btnExport.addEventListener('click', async function(){
      try{
        // Fetch all products from server
        const response = await fetch('/sales/commercial-dashboard/table/?export=all', {
          headers: {'X-Requested-With': 'XMLHttpRequest'}
        });
        const html = await response.text();
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = html;
        const rows = Array.from(tempDiv.querySelectorAll('tbody tr[data-product-id]'));
        
        if(!rows.length){ alert('Aucune ligne à exporter.'); return; }
        
        const header = ['ID','Produit','Marque','Prix Achat','Prix Gros','Prix Vente'];
        const data = rows.map(r=>{
          const id = r.getAttribute('data-product-id');
          const name = (r.querySelector('td:nth-child(2) div:first-child')?.textContent||'').trim();
          const brand = (r.querySelector('td:nth-child(2) .brand-badge')?.textContent||'').trim();
          const cost = (r.querySelector('td:nth-child(3) span')?.textContent||'').trim();
          const wholesale = (r.querySelector('td:nth-child(4) span')?.textContent||'').trim();
          const selling = (r.querySelector('td:nth-child(5) span')?.textContent||'').trim();
          return [id, name, brand, cost, wholesale, selling];
        });
        
        // Create Excel content
        const excelContent = createExcelContent([header, ...data]);
        const blob = new Blob([excelContent], {type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'produits.xlsx';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }catch(e){ alert('Export impossible: ' + e.message); }
    });
  }
  
  // Create Excel content via HTML table (works with Excel when saved .xls)
  function createExcelContent(data) {
    const table = `<table>${data.map(row => `<tr>${row.map(cell => `<td>${String(cell).replace(/&/g,'&amp;').replace(/</g,'&lt;')}</td>`).join('')}</tr>`).join('')}</table>`;
    const html = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>${table}</body></html>`;
    return html;
  }

  // Restock modal buttons
  const btnMoto = document.getElementById('btnRestockMoto');
  const btnPiece = document.getElementById('btnRestockPiece');
  if (btnMoto) btnMoto.addEventListener('click', (e)=>{ e.preventDefault(); try{ openRestockModalUI('moto'); }catch(_){} });
  if (btnPiece) btnPiece.addEventListener('click', (e)=>{ e.preventDefault(); try{ openRestockModalUI('piece'); }catch(_){} });
});

// Price modal handlers
function openPriceModal(id, name, brand, cost, wholesale, selling){
  const m=document.getElementById('priceModal');
  m.querySelector('#pm_name').textContent = name;
  m.querySelector('#pm_brand').textContent = brand||'—';
  m.querySelector('#pm_product_id').value = id;
  m.querySelector('#pm_cost').value = cost||0;
  m.querySelector('#pm_wholesale').value = wholesale||0;
  m.querySelector('#pm_selling').value = selling||0;
  m.style.display='block';
}
function closePriceModal(){ document.getElementById('priceModal').style.display='none'; }
async function submitPriceModal(){
  const id = document.getElementById('pm_product_id').value;
  const payload = {
    product_id: Number(id),
    cost_price: Number(document.getElementById('pm_cost').value||0),
    wholesale_price: Number(document.getElementById('pm_wholesale').value||0),
    selling_price: Number(document.getElementById('pm_selling').value||0)
  };
  try{
    const url = document.getElementById('priceModal').dataset.apiPrice;
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken': __csrftoken}, credentials:'same-origin', body: JSON.stringify(payload)});
    const res = await r.json();
    if(r.ok && res.ok){ 
      closePriceModal();
      showNotice('Succès', 'La demande de modification de prix a été envoyée pour approbation.', 'success');
    }
    else{ 
      showNotice('Erreur', res.error||'Erreur lors de la demande.', 'error');
    }
  }catch(e){ alert('Erreur réseau.'); }
}

// Restock modal handlers (namespaced to avoid collisions with template inline functions)
function openRestockModalUI(kind){
  console.log('Opening restock modal for kind:', kind);
  const m=document.getElementById('restockModal');
  if(!m){ console.error('restockModal not found'); return; }
  console.log('Modal element found:', m);
  
  const kindInput = m.querySelector('#rm_kind');
  if(kindInput){ kindInput.value = kind; }
  
  m.style.display='block';
  m.removeAttribute('aria-hidden');
  m.style.visibility='visible';
  m.style.zIndex = '1000';
  
  console.log('Modal display style:', m.style.display);
  console.log('Modal visibility:', m.style.visibility);
  
  // Initialize the modal with the new functionality
  if (typeof window.initRestockModal === 'function') {
    console.log('Calling initRestockModal');
    window.initRestockModal(kind);
  } else {
    console.error('initRestockModal function not found');
  }
}
function closeRestockModalUI(){ 
  const m = document.getElementById('restockModal');
  if(!m) return;
  m.style.display='none'; 
  m.setAttribute('aria-hidden','true');
  m.style.visibility='hidden';
  // Reset the modal
  if (typeof window.closeRestockModal === 'function') {
    window.closeRestockModal();
  }
}
async function submitRestockUI(){
  // This will be handled by the new modal JavaScript
  if (typeof window.submitRestock === 'function') {
    await window.submitRestock();
  }
}

// Notification card renderer
function showNotice(title, message, level){
  const container = document.getElementById('noticeContainer');
  if(!container) return;
  const card = document.createElement('div');
  card.className = 'notice-card' + (level ? ' ' + level : '');
  card.innerHTML = `
    <div class="notice-title">${title}</div>
    <div class="notice-message">${message}</div>
    <button class="notice-close" aria-label="Fermer">✕</button>
  `;
  const closeBtn = card.querySelector('.notice-close');
  closeBtn.addEventListener('click', ()=>{ card.remove(); });
  container.innerHTML = '';
  container.appendChild(card);
  // Auto dismiss after 6 seconds
  setTimeout(()=>{ if(card && card.parentNode){ card.remove(); } }, 6000);
}



