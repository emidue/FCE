/* ─────────────────────────────────────────────
   VetFactura C — app.js
   Frontend · Integra con FastAPI backend
───────────────────────────────────────────── */

const API = 'http://localhost:8000'; // URL del backend FastAPI

// ═══════════════════════════════════════════
// Estado global
// ═══════════════════════════════════════════
const state = {
  items:    [],
  clientes: [],
  lastResult: null,
  configCache: null,
};

// ═══════════════════════════════════════════
// Init
// ═══════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  setHoy();
  addItem();
  setupNav();
  setupButtons();
  setupConceptoToggle();
  setupContactoToggle();
  setupSortableHeaders();
  cargarClientes();
  cargarHistorial();
  cargarConfig();
  verificarToken();
});

function localDate() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function setHoy() {
  const hoy = localDate();
  document.getElementById('fechaCbte').value = hoy;
  document.getElementById('fchServDesde').value = hoy;
  document.getElementById('fchServHasta').value = hoy;
  document.getElementById('fchVtoPago').value = hoy;
}

function setupConceptoToggle() {
  const sel = document.getElementById('concepto');
  const row = document.getElementById('rowFechasServ');
  const btn = document.getElementById('btnToggleFechas');
  const needsFechas = () => sel.value !== '1';

  const sync = () => {
    if (!needsFechas()) {
      row.classList.remove('open');
      btn.classList.remove('active');
      btn.style.display = 'none';
    } else {
      btn.style.display = '';
    }
  };

  btn.addEventListener('click', () => {
    row.classList.toggle('open');
    btn.classList.toggle('active');
  });

  sel.addEventListener('change', sync);
  sync();
}

function setupContactoToggle() {
  const btn = document.getElementById('btnToggleContacto');
  const row = document.getElementById('rowContacto');
  btn.addEventListener('click', () => {
    row.classList.toggle('open');
    btn.classList.toggle('active');
  });
}

// ═══════════════════════════════════════════
// Navegación
// ═══════════════════════════════════════════
function setupNav() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const sec = btn.dataset.section;
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      document.getElementById(`page-${sec}`).classList.add('active');
      if (sec === 'historial') cargarHistorial();
      if (sec === 'clientes')  cargarClientes();
      if (sec === 'config')    cargarConfig();
    });
  });
}

// ═══════════════════════════════════════════
// Botones
// ═══════════════════════════════════════════
function setupButtons() {
  document.getElementById('btnAddItem').addEventListener('click', addItem);
  document.getElementById('btnEmitir').addEventListener('click', emitir);
  document.getElementById('btnPreview').addEventListener('click', mostrarPreview);
  document.getElementById('btnClosePreview').addEventListener('click', () => closeOverlay('ovPreview'));
  document.getElementById('btnEmitirPreview').addEventListener('click', () => { closeOverlay('ovPreview'); emitir(); });
  document.getElementById('btnNueva').addEventListener('click', () => { closeOverlay('ovExito'); resetForm(); });
  document.getElementById('btnPDF').addEventListener('click', descargarPDF);
  document.getElementById('btnNuevoCliente').addEventListener('click', () => abrirModalCliente());
  document.getElementById('btnCancelCliente').addEventListener('click', () => closeOverlay('ovCliente'));
  document.getElementById('btnGuardarCliente').addEventListener('click', guardarCliente);

  // Cerrar overlay al click fuera
  document.querySelectorAll('.overlay').forEach(ov => {
    ov.addEventListener('click', e => { if (e.target === ov) closeOverlay(ov.id); });
  });

  // Filtro historial
  document.getElementById('filtroHist').addEventListener('input', filtrarHistorial);

  // Formateo visual del nro de documento en Nueva Factura
  const nroDocInput = document.getElementById('nroDoc');
  const tipoDocSel  = document.getElementById('tipoDoc');
  function formatearNroDocInput() {
    const raw = nroDocInput.value.replace(/\D/g, '');
    nroDocInput.value = raw ? fmtDoc(raw, tipoDocSel.value) : '';
  }
  nroDocInput.addEventListener('input', formatearNroDocInput);
  tipoDocSel.addEventListener('change', formatearNroDocInput);
}

// ═══════════════════════════════════════════
// Ítems
// ═══════════════════════════════════════════
function addItem(datos = {}) {
  const id = Date.now();
  state.items.push({ id, desc: datos.desc || '', qty: datos.qty || 1, price: datos.price || 0 });

  const row = document.createElement('div');
  row.className = 'item-row';
  row.dataset.id = id;
  row.innerHTML = `
    <input type="text" class="input" placeholder="Ej: Consulta + vacuna" data-field="desc" />
    <input type="number" class="input" value="1" min="1" step="1" data-field="qty" />
    <input type="number" class="input" placeholder="0,00" min="0" step="0.01" data-field="price" />
    <span class="item-sub" data-sub="${id}">$ 0,00</span>
    <button type="button" class="btn-del" title="Eliminar">✕</button>
  `;

  if (datos.desc)  row.querySelector('[data-field="desc"]').value  = datos.desc;
  if (datos.qty)   row.querySelector('[data-field="qty"]').value   = datos.qty;
  if (datos.price) row.querySelector('[data-field="price"]').value = datos.price;

  row.querySelectorAll('.input').forEach(inp => {
    inp.addEventListener('input', () => updateItem(id, inp.dataset.field, inp.value));
  });
  row.querySelector('.btn-del').addEventListener('click', () => removeItem(id));

  document.getElementById('itemsContainer').appendChild(row);
  recalc();
}

function updateItem(id, field, val) {
  const item = state.items.find(i => i.id === id);
  if (!item) return;
  item[field] = field === 'desc' ? val : parseFloat(val) || 0;
  recalc();
}

function removeItem(id) {
  if (state.items.length === 1) { toast('Debe haber al menos un ítem.', 'warn'); return; }
  state.items = state.items.filter(i => i.id !== id);
  document.querySelector(`.item-row[data-id="${id}"]`)?.remove();
  recalc();
}

function recalc() {
  let total = 0;
  state.items.forEach(item => {
    const sub = item.qty * item.price;
    total += sub;
    const el = document.querySelector(`[data-sub="${item.id}"]`);
    if (el) el.textContent = fmt(sub);
  });
  document.getElementById('totalBruto').textContent = fmt(total);
  document.getElementById('totalFinal').textContent = fmt(total);
}

// ═══════════════════════════════════════════
// Validación
// ═══════════════════════════════════════════
function validar() {
  const errs = [];
  const nombre = document.getElementById('receptorNombre').value.trim();
  if (!nombre) errs.push('Nombre del cliente obligatorio.');
  if (state.items.every(i => !i.desc || i.price === 0))
    errs.push('Completá al menos un ítem con descripción y precio.');
  if (errs.length) { toast(errs.join(' '), 'error'); return false; }
  return true;
}

// ═══════════════════════════════════════════
// Emitir Factura C
// ═══════════════════════════════════════════
async function emitir() {
  if (!validar()) return;

  const steps = ['s1','s2','s3'].map(id => document.getElementById(id));
  steps.forEach(s => s.classList.remove('active','done'));
  openOverlay('ovEmitiendo');

  const payload = buildPayload();

  try {
    // — Paso 1: WSAA auth —
    await step(steps[0], async () => {
      // El backend maneja el token internamente; simulamos el delay visual
      await sleep(1100);
    });

    // — Paso 2: Consultar último nro —
    await step(steps[1], () => sleep(1300));

    // — Paso 3: FECAESolicitar —
    const result = await step(steps[2], async () => {
      const res = await fetch(`${API}/facturas/emitir`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Error ${res.status}`);
      }
      return res.json();
    });

    closeOverlay('ovEmitiendo');
    state.lastResult = result;
    mostrarExito(result);
    agregarAlHistorial(result, payload);
    resetForm();

  } catch (e) {
    closeOverlay('ovEmitiendo');
    toast(`Error ARCA: ${e.message}`, 'error');
  }
}

function buildPayload() {
  return {
    fecha_cbte:      document.getElementById('fechaCbte').value.replace(/-/g,''),  // YYYYMMDD
    concepto:        parseInt(document.getElementById('concepto').value),
    tipo_doc:        parseInt(document.getElementById('tipoDoc').value),
    nro_doc:         document.getElementById('nroDoc').value.replace(/\D/g,'') || '0',
    cond_iva_receptor: parseInt(document.getElementById('condIva').value),
    fch_serv_desde:  document.getElementById('fchServDesde').value.replace(/-/g,''),
    fch_serv_hasta:  document.getElementById('fchServHasta').value.replace(/-/g,''),
    fch_vto_pago:    document.getElementById('fchVtoPago').value.replace(/-/g,''),
    receptor_nombre: document.getElementById('receptorNombre').value.trim(),
    receptor_email:  document.getElementById('receptorEmail').value.trim(),
    receptor_dom:    document.getElementById('receptorDom').value.trim(),
    cond_venta:      document.getElementById('condVenta').value,
    observaciones:   document.getElementById('observaciones').value.trim(),
    items: state.items.filter(i => i.desc && i.price > 0).map(i => ({
      descripcion: i.desc,
      cantidad:    i.qty,
      precio_unit: i.price,
    })),
    imp_total: state.items.reduce((s, i) => s + i.qty * i.price, 0),
  };
}

async function step(el, fn) {
  el.classList.add('active');
  const res = await fn();
  el.classList.remove('active');
  el.classList.add('done');
  el.textContent = '✓ ' + el.textContent.replace(/^[①②③]\s/,'');
  return res;
}

// ═══════════════════════════════════════════
// Éxito
// ═══════════════════════════════════════════
function mostrarExito(r) {
  document.getElementById('resultBox').innerHTML = `
    <div><strong>Comprobante:</strong> ${fmtNro(r.punto_venta, r.nro_cbte)}</div>
    <div><strong>Tipo:</strong> Factura C</div>
    <div><strong>Cliente:</strong> ${r.receptor_nombre || '—'}</div>
    <div><strong>Total:</strong> ${fmt(r.imp_total)}</div>
    <div><strong>CAE:</strong> ${r.cae}</div>
    <div><strong>Vto. CAE:</strong> ${fmtFecha(r.vto_cae)}</div>
  `;
  openOverlay('ovExito');
}

// ═══════════════════════════════════════════
// Historial (local + API)
// ═══════════════════════════════════════════
function agregarAlHistorial(r, payload) {
  const tbody = document.getElementById('historialBody');
  const emptyRow = tbody.querySelector('.empty-row');
  if (emptyRow) emptyRow.remove();

  const tr = document.createElement('tr');
  const email = (payload.receptor_email || '').replace(/"/g,'&quot;');
  tr.innerHTML = `
    <td>${fmtNro(r.punto_venta, r.nro_cbte)}</td>
    <td>${fmtFecha(payload.fecha_cbte)}</td>
    <td>${payload.receptor_nombre || '—'}</td>
    <td>${fmt(r.imp_total)}</td>
    <td class="cae-text">${r.cae}</td>
    <td>${fmtFecha(r.vto_cae)}</td>
    <td>
      <button class="btn-sm" onclick="descargarPDFById(${r.id})">⬇ PDF</button>
      <button class="btn-sm" onclick="enviarFacturaPorEmail(${r.id}, '${email}')">✉ Email</button>
      <button class="btn-sm" onclick="enviarFacturaPorWhatsapp(${r.id})">🟢 WhatsApp</button>
    </td>
  `;
  tbody.insertBefore(tr, tbody.firstChild);
  actualizarTotalHoy(r.imp_total);
}

let totalHoy = 0;
function actualizarTotalHoy(monto) {
  totalHoy += monto;
  document.getElementById('totalHoy').textContent = fmt(totalHoy);
}

const PAGE_SIZE = 10;

// ═══════════════════════════════════════════
// Paginador y ordenamiento genéricos
// ═══════════════════════════════════════════
function renderPaginador(containerId, totalPages, currentPage, onPage) {
  const el = document.getElementById(containerId);
  el.innerHTML = '';
  if (totalPages <= 1) return;

  const prev = document.createElement('button');
  prev.textContent = '‹';
  prev.disabled = currentPage <= 1;
  prev.onclick = () => onPage(currentPage - 1);
  el.appendChild(prev);

  // Show page numbers with ellipsis
  const pages = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || (i >= currentPage - 1 && i <= currentPage + 1)) {
      pages.push(i);
    } else if (pages[pages.length - 1] !== '...') {
      pages.push('...');
    }
  }
  pages.forEach(p => {
    if (p === '...') {
      const sp = document.createElement('span');
      sp.className = 'pag-info';
      sp.textContent = '...';
      el.appendChild(sp);
    } else {
      const btn = document.createElement('button');
      btn.textContent = p;
      if (p === currentPage) btn.className = 'active';
      btn.onclick = () => onPage(p);
      el.appendChild(btn);
    }
  });

  const next = document.createElement('button');
  next.textContent = '›';
  next.disabled = currentPage >= totalPages;
  next.onclick = () => onPage(currentPage + 1);
  el.appendChild(next);

  const info = document.createElement('span');
  info.className = 'pag-info';
  info.textContent = `Pág. ${currentPage} de ${totalPages}`;
  el.appendChild(info);
}

function updateSortHeaders(tableId, sortState) {
  const table = document.getElementById(tableId);
  if (!table) return;
  table.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('asc', 'desc');
    if (th.dataset.col === sortState.col) th.classList.add(sortState.dir);
  });
}

function setupSortableHeaders() {
  // Historial
  document.querySelectorAll('#tablaHistorial th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (state.histSort.col === col) {
        state.histSort.dir = state.histSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.histSort = { col, dir: 'asc' };
      }
      state.histPage = 1;
      renderHistorial();
    });
  });
  // Clientes
  document.querySelectorAll('#tablaClientes th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (state.cliSort.col === col) {
        state.cliSort.dir = state.cliSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.cliSort = { col, dir: 'asc' };
      }
      state.cliPage = 1;
      renderClientes();
    });
  });
  // Filtro clientes
  document.getElementById('filtroClientes').addEventListener('input', e => {
    state.cliFilter = e.target.value;
    state.cliPage = 1;
    renderClientes();
  });
}

state.histData = [];
state.histPage = 1;
state.histSort = { col: null, dir: 'desc' };
state.histFilter = '';

async function cargarHistorial() {
  try {
    const res = await fetch(`${API}/facturas`);
    if (!res.ok) return;
    state.histData = await res.json();
    // Calcular total hoy
    const hoy = localDate().replace(/-/g,'');
    totalHoy = 0;
    state.histData.forEach(r => {
      if (String(r.fecha_cbte).replace(/-/g,'') === hoy) totalHoy += Number(r.imp_total) || 0;
    });
    document.getElementById('totalHoy').textContent = fmt(totalHoy);
    state.histPage = 1;
    renderHistorial();
  } catch (_) {}
}

function renderHistorial() {
  let data = state.histData;
  // Filtrar
  if (state.histFilter) {
    const q = state.histFilter.toLowerCase();
    data = data.filter(r => `${fmtNro(r.punto_venta,r.nro_cbte)} ${r.receptor_nombre||''} ${r.cae||''}`.toLowerCase().includes(q));
  }
  // Ordenar
  if (state.histSort.col) {
    const col = state.histSort.col;
    const dir = state.histSort.dir === 'asc' ? 1 : -1;
    data = [...data].sort((a, b) => {
      let va = a[col] ?? '', vb = b[col] ?? '';
      if (typeof va === 'number') return (va - vb) * dir;
      return String(va).localeCompare(String(vb), 'es', {numeric: true}) * dir;
    });
  }
  // Paginar
  const total = data.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (state.histPage > totalPages) state.histPage = totalPages;
  const start = (state.histPage - 1) * PAGE_SIZE;
  const page = data.slice(start, start + PAGE_SIZE);

  const tbody = document.getElementById('historialBody');
  tbody.innerHTML = '';
  if (!total) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No hay comprobantes emitidos aún.</td></tr>';
  } else {
    page.forEach(r => {
      const tr = document.createElement('tr');
      const email = (r.receptor_email || '').replace(/"/g,'&quot;');
      tr.innerHTML = `
        <td>${fmtNro(r.punto_venta, r.nro_cbte)}</td>
        <td>${fmtFecha(r.fecha_cbte)}</td>
        <td>${r.receptor_nombre || '—'}</td>
        <td>${fmt(r.imp_total)}</td>
        <td class="cae-text">${r.cae || '—'}</td>
        <td>${r.vto_cae ? fmtFecha(r.vto_cae) : '—'}</td>
        <td>
          <button class="btn-sm" onclick="descargarPDFById(${r.id})">⬇ PDF</button>
          <button class="btn-sm" onclick="enviarFacturaPorEmail(${r.id}, '${email}')">✉ Email</button>
          <button class="btn-sm" onclick="enviarFacturaPorWhatsapp(${r.id})">🟢 WhatsApp</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }
  renderPaginador('paginadorHist', totalPages, state.histPage, p => { state.histPage = p; renderHistorial(); });
  updateSortHeaders('tablaHistorial', state.histSort);
}

function filtrarHistorial() {
  state.histFilter = document.getElementById('filtroHist').value;
  state.histPage = 1;
  renderHistorial();
}

// ═══════════════════════════════════════════
// Clientes
// ═══════════════════════════════════════════
state.cliPage = 1;
state.cliSort = { col: 'nombre', dir: 'asc' };
state.cliFilter = '';

async function cargarClientes() {
  try {
    const res = await fetch(`${API}/clientes`);
    if (!res.ok) return;
    state.clientes = await res.json();
    state.cliPage = 1;
    renderClientes();
  } catch (_) {}
}

function renderClientes() {
  const ivaLabel = { 5:'Cons. Final', 1:'Resp. Inscripto', 4:'Exento', 6:'Monotributo', 7:'No Categorizado', 8:'Prov. Exterior', 9:'Cliente Exterior', 10:'Liberado', 13:'Monotrib. Social', 15:'No Alcanzado', 16:'Monotrib. Promovido' };
  const tipoLabel = { 96:'DNI', 86:'CUIL', 80:'CUIT', 99:'Sin ident.' };
  let data = state.clientes;
  // Filtrar
  if (state.cliFilter) {
    const q = state.cliFilter.toLowerCase();
    data = data.filter(c => `${c.nombre} ${c.nro_doc} ${c.email||''}`.toLowerCase().includes(q));
  }
  // Ordenar
  if (state.cliSort.col) {
    const col = state.cliSort.col;
    const dir = state.cliSort.dir === 'asc' ? 1 : -1;
    data = [...data].sort((a, b) => {
      let va = a[col] ?? '', vb = b[col] ?? '';
      if (typeof va === 'number') return (va - vb) * dir;
      return String(va).localeCompare(String(vb), 'es', {numeric: true}) * dir;
    });
  }
  // Paginar
  const total = data.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (state.cliPage > totalPages) state.cliPage = totalPages;
  const start = (state.cliPage - 1) * PAGE_SIZE;
  const page = data.slice(start, start + PAGE_SIZE);

  const tbody = document.getElementById('clientesBody');
  tbody.innerHTML = '';
  if (!total) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No hay clientes guardados.</td></tr>';
  } else {
    page.forEach(c => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${c.nombre}</td>
        <td>${tipoLabel[c.tipo_doc] || '—'}</td>
        <td>${fmtDoc(c.nro_doc, c.tipo_doc)}</td>
        <td>${ivaLabel[c.cond_iva] || '—'}</td>
        <td>${c.email || '—'}</td>
        <td>${c.cant_facturas || 0}</td>
        <td class="acciones-cell">
          <button class="btn-sm" onclick="usarCliente(${c.id})" title="Facturar">Facturar</button>
          <button class="btn-sm btn-sm--edit" onclick="editarCliente(${c.id})" title="Editar">✎</button>
          <button class="btn-sm btn-sm--del" onclick="eliminarCliente(${c.id}, '${c.nombre.replace(/'/g, "\\'")}')" title="Eliminar">✕</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }
  renderPaginador('paginadorClientes', totalPages, state.cliPage, p => { state.cliPage = p; renderClientes(); });
  updateSortHeaders('tablaClientes', state.cliSort);
}

function usarCliente(id) {
  const c = state.clientes.find(x => x.id === id);
  if (!c) return;
  document.getElementById('receptorNombre').value = c.nombre;
  document.getElementById('tipoDoc').value = c.tipo_doc;
  document.getElementById('nroDoc').value = fmtDoc(c.nro_doc, c.tipo_doc);
  document.getElementById('condIva').value = c.cond_iva || 5;
  if (c.email) document.getElementById('receptorEmail').value = c.email;
  if (c.domicilio) document.getElementById('receptorDom').value = c.domicilio;
  if (c.email || c.domicilio) {
    document.getElementById('rowContacto').classList.add('open');
    document.getElementById('btnToggleContacto').classList.add('active');
  }
  // Ir a nueva factura
  document.querySelector('[data-section="nueva"]').click();
  toast(`Cliente "${c.nombre}" cargado.`);
}

function abrirModalCliente(id = null) {
  const c = id ? state.clientes.find(x => x.id === id) : null;
  document.getElementById('ncId').value = c ? c.id : '';
  document.getElementById('ncTitle').textContent = c ? 'Editar Cliente' : 'Nuevo Cliente';
  document.getElementById('ncNombre').value  = c ? c.nombre : '';
  document.getElementById('ncTipoDoc').value = c ? c.tipo_doc : '96';
  document.getElementById('ncNroDoc').value  = c ? c.nro_doc : '';
  document.getElementById('ncCondIva').value = c ? (c.cond_iva || 5) : '5';
  document.getElementById('ncEmail').value      = c ? (c.email || '') : '';
  document.getElementById('ncDomicilio').value  = c ? (c.domicilio || '') : '';
  openOverlay('ovCliente');
}

function editarCliente(id) {
  abrirModalCliente(id);
}

async function eliminarCliente(id, nombre) {
  if (!confirm(`¿Eliminar al cliente "${nombre}"?`)) return;
  try {
    const res = await fetch(`${API}/clientes/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error();
    await cargarClientes();
    toast(`Cliente "${nombre}" eliminado.`);
  } catch (_) {
    toast('Error al eliminar cliente.', 'warn');
  }
}

async function guardarCliente() {
  const editId  = document.getElementById('ncId').value;
  const nombre  = document.getElementById('ncNombre').value.trim();
  const tipoDoc = parseInt(document.getElementById('ncTipoDoc').value);
  const nroDoc  = document.getElementById('ncNroDoc').value.replace(/\D/g, '');
  const condIva = parseInt(document.getElementById('ncCondIva').value);
  const email   = document.getElementById('ncEmail').value.trim();
  if (!nombre) { toast('El nombre es obligatorio.', 'warn'); return; }

  const domicilio = document.getElementById('ncDomicilio').value.trim();
  const payload = { nombre, tipo_doc: tipoDoc, nro_doc: nroDoc, cond_iva: condIva, email, domicilio };

  try {
    let res;
    if (editId) {
      res = await fetch(`${API}/clientes/${editId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } else {
      res = await fetch(`${API}/clientes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    }
    if (!res.ok) throw new Error();
    closeOverlay('ovCliente');
    await cargarClientes();
    toast(`Cliente "${nombre}" ${editId ? 'actualizado' : 'guardado'}.`);
  } catch (_) {
    toast('Error al guardar cliente.', 'warn');
  }
}

// ═══════════════════════════════════════════
// Preview
// ═══════════════════════════════════════════
function mostrarPreview() {
  const cfg = getConfig();
  document.getElementById('pvEmisor').textContent = cfg.razon;
  document.getElementById('pvCuit').textContent   = `CUIT: ${fmtDoc(cfg.cuit, 80)}`;
  document.getElementById('pvNro').textContent    = fmtNro(document.getElementById('puntoVenta').value, '??????????');
  document.getElementById('pvCliente').textContent = document.getElementById('receptorNombre').value || '—';
  document.getElementById('pvDoc').textContent =
    document.getElementById('tipoDoc').options[document.getElementById('tipoDoc').selectedIndex].text +
    ': ' + fmtDoc(document.getElementById('nroDoc').value, document.getElementById('tipoDoc').value);
  document.getElementById('pvFecha').textContent = fmtFecha(document.getElementById('fechaCbte').value.replace(/-/g,''));

  const tbl = document.getElementById('pvTable');
  tbl.innerHTML = `
    <thead><tr><th>Descripción</th><th>Cant.</th><th>Precio</th><th>Subtotal</th></tr></thead>
    <tbody>
      ${state.items.filter(i => i.desc).map(i => `
        <tr>
          <td>${i.desc}</td>
          <td>${i.qty}</td>
          <td>${fmt(i.price)}</td>
          <td>${fmt(i.qty * i.price)}</td>
        </tr>
      `).join('')}
    </tbody>
  `;
  document.getElementById('pvTotal').textContent = document.getElementById('totalFinal').textContent;
  openOverlay('ovPreview');
}

// ═══════════════════════════════════════════
// PDF
// ═══════════════════════════════════════════
async function descargarPDF() {
  if (!state.lastResult?.id) {
    alert('El PDF se genera desde el backend. URL: GET /facturas/{id}/pdf');
    return;
  }
  descargarPDFById(state.lastResult.id);
}

async function descargarPDFById(id) {
  try {
    const res = await fetch(`${API}/facturas/${id}/pdf`);
    if (!res.ok) throw new Error();
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `factura_c_${id}.pdf`;
    a.click();
  } catch (_) {
    toast('PDF no disponible (backend no conectado).', 'warn');
  }
}

// ═══════════════════════════════════════════
// Token WSAA
// ═══════════════════════════════════════════
async function verificarToken() {
  try {
    const res = await fetch(`${API}/wsaa/estado`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.expira) {
      document.getElementById('tokenVto').textContent = fmtFecha(data.expira) + ' ' + (data.hora || '');
    }
    // Badge del ambiente (soporta ambos casos al cambiar en caliente)
    const statusText = document.querySelector('.status-text');
    if (statusText && data.ambiente) {
      statusText.textContent = data.ambiente === 'produccion'
        ? 'ARCA · Producción'
        : 'ARCA · Homologación';
    }
  } catch (_) { /* backend no disponible */ }
}

async function renovarToken() {
  try {
    const res = await fetch(`${API}/wsaa/renovar`, { method: 'POST' });
    if (!res.ok) throw new Error();
    toast('Token WSAA renovado.');
    await verificarToken();
  } catch (_) {
    toast('No se pudo renovar el token (backend no conectado).', 'warn');
  }
}

// ═══════════════════════════════════════════
// Config
// ═══════════════════════════════════════════
function getConfig() {
  const c = state.configCache || {};
  const e = c.emisor || {};
  return {
    razon: e.razon_social || 'Sin configurar',
    cuit:  c.cuit || '',
  };
}

async function cargarConfig() {
  try {
    const res = await fetch(`${API}/config`);
    if (!res.ok) return;
    const cfg = await res.json();
    state.configCache = cfg;
    const e = cfg.emisor || {};
    const s = cfg.smtp || {};
    const nombreSistema = cfg.nombre_sistema || 'VetFactura';
    document.getElementById('cfgNombreSistema').value = nombreSistema;
    document.getElementById('brandName').textContent  = nombreSistema;
    document.title = `${nombreSistema} — Facturación Electrónica`;
    document.getElementById('cfgRazon').value        = e.razon_social || '';
    const cuitEl = document.getElementById('cfgCuit');
    if (cuitEl) cuitEl.value = cfg.cuit || '';
    const pvEl = document.getElementById('cfgPuntoVenta');
    if (pvEl) pvEl.value = cfg.punto_venta || 1;
    // El punto de venta del formulario de emisión refleja siempre el configurado
    const pvFactura = document.getElementById('puntoVenta');
    if (pvFactura) pvFactura.value = cfg.punto_venta || 1;
    // Badge en la tarjeta de Certificados ARCA
    const pvBadge = document.getElementById('certPuntoVenta');
    if (pvBadge) pvBadge.textContent = String(cfg.punto_venta || 1).padStart(4, '0') + ' · Web Services';
    // Ambiente (select en Certificados ARCA + badge del header)
    const ambSel = document.getElementById('cfgAmbiente');
    if (ambSel) ambSel.value = cfg.ambiente || 'homologacion';
    const statusText = document.querySelector('.status-text');
    if (statusText) {
      statusText.textContent = cfg.ambiente === 'produccion'
        ? 'ARCA · Producción'
        : 'ARCA · Homologación';
    }
    // Rutas de certs por ambiente + status de existencia
    const certs  = cfg.certs  || {};
    const status = cfg.certs_status || {};
    const setPath = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
    const setStat = (id, exists, empty) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (empty) { el.textContent = ''; return; }
      el.textContent = exists ? '· ✓ encontrado' : '· ✗ no encontrado';
      el.style.color = exists ? '#2e7d32' : '#c0392b';
    };
    const h = certs.homologacion || {};
    const p = certs.produccion   || {};
    const hs = status.homologacion || {};
    const ps = status.produccion   || {};
    setPath('cfgCertHomo', h.cert_path);
    setPath('cfgKeyHomo',  h.key_path);
    setPath('cfgCertProd', p.cert_path);
    setPath('cfgKeyProd',  p.key_path);
    setStat('cfgCertHomoStatus', hs.cert_exists, !h.cert_path);
    setStat('cfgKeyHomoStatus',  hs.key_exists,  !h.key_path);
    setStat('cfgCertProdStatus', ps.cert_exists, !p.cert_path);
    setStat('cfgKeyProdStatus',  ps.key_exists,  !p.key_path);
    document.getElementById('cfgDom').value          = e.domicilio || '';
    const domCom = document.getElementById('cfgDomCom');
    if (domCom) domCom.value = e.domicilio_comercial || '';
    const iibb = document.getElementById('cfgIIBB');
    if (iibb) iibb.value = e.ingresos_brutos || '';
    const iniAct = document.getElementById('cfgInicioAct');
    if (iniAct) {
      const v = e.inicio_actividades || '';
      iniAct.value = /^\d{8}$/.test(v) ? `${v.slice(0,4)}-${v.slice(4,6)}-${v.slice(6)}` : v;
    }
    const condSel = document.getElementById('cfgCondIva');
    if (condSel && e.condicion_iva) condSel.value = e.condicion_iva;
    document.getElementById('cfgSmtpHost').value     = s.host || '';
    document.getElementById('cfgSmtpPort').value     = s.port || 587;
    document.getElementById('cfgSmtpTls').value      = String(s.use_tls !== false);
    document.getElementById('cfgSmtpUser').value     = s.user || '';
    document.getElementById('cfgSmtpPass').value     = '';
    document.getElementById('cfgSmtpPass').placeholder = s.password ? '•••••• (guardada)' : 'Dejar vacío para conservar';
    document.getElementById('cfgSmtpFromEmail').value= s.from_email || '';
    document.getElementById('cfgSmtpFromName').value = s.from_name || 'VetFactura';

    // Logo
    const img   = document.getElementById('cfgLogoPreview');
    const empty = document.getElementById('cfgLogoEmpty');
    if (cfg.has_logo) {
      img.src = `${API}/config/logo?t=${Date.now()}`;
      img.style.display = '';
      empty.style.display = 'none';
    } else {
      img.style.display = 'none';
      empty.style.display = '';
    }
  } catch (_) { /* backend no disponible */ }
}

async function guardarConfig() {
  const nombreSistema = document.getElementById('cfgNombreSistema').value.trim() || 'VetFactura';
  const pvRaw = parseInt(document.getElementById('cfgPuntoVenta')?.value, 10);
  const ambiente = document.getElementById('cfgAmbiente')?.value || null;
  const val = id => (document.getElementById(id)?.value || '').trim();
  const payload = {
    nombre_sistema: nombreSistema,
    punto_venta: Number.isFinite(pvRaw) && pvRaw > 0 ? pvRaw : null,
    ambiente,
    certs: {
      homologacion: { cert_path: val('cfgCertHomo'), key_path: val('cfgKeyHomo') },
      produccion:   { cert_path: val('cfgCertProd'), key_path: val('cfgKeyProd') },
    },
    emisor: {
      razon_social:        document.getElementById('cfgRazon').value.trim(),
      domicilio:           document.getElementById('cfgDom').value.trim(),
      domicilio_comercial: (document.getElementById('cfgDomCom')?.value || '').trim(),
      condicion_iva:       document.getElementById('cfgCondIva').value,
      cuit:                (document.getElementById('cfgCuit')?.value || '').trim(),
      ingresos_brutos:     (document.getElementById('cfgIIBB')?.value || '').trim(),
      inicio_actividades:  (document.getElementById('cfgInicioAct')?.value || '').replace(/-/g,''),
    },
    smtp: {
      host:       document.getElementById('cfgSmtpHost').value.trim(),
      port:       parseInt(document.getElementById('cfgSmtpPort').value) || 587,
      user:       document.getElementById('cfgSmtpUser').value.trim(),
      password:   document.getElementById('cfgSmtpPass').value,
      from_email: document.getElementById('cfgSmtpFromEmail').value.trim(),
      from_name:  document.getElementById('cfgSmtpFromName').value.trim() || nombreSistema,
      use_tls:    document.getElementById('cfgSmtpTls').value === 'true',
    },
  };
  try {
    const res = await fetch(`${API}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'Error');
    toast('Configuración guardada.');
    cargarConfig();
  } catch (e) {
    toast(`No se pudo guardar: ${e.message}`, 'error');
  }
}

async function checkStatus() {
  const ids = ['stWsaa', 'stWsfe', 'stAuth', 'stCert', 'stKey'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    el.className = 'pill pill--load';
    el.textContent = 'Verificando...';
  });
  try {
    const res = await fetch(`${API}/status`);
    if (!res.ok) throw new Error('Error al consultar');
    const st = await res.json();

    const set = (id, ok, okText, errText) => {
      const el = document.getElementById(id);
      el.className = ok ? 'pill pill--ok' : 'pill pill--err';
      el.textContent = ok ? okText : errText;
    };

    set('stWsaa', st.wsaa?.ok, 'Operativo', `Caído (${st.wsaa?.code || 'sin conexión'})`);
    set('stWsfe', st.wsfe?.ok, 'Operativo', `Caído (${st.wsfe?.code || 'sin conexión'})`);

    if (st.wsaa_auth?.ok) {
      const exp = st.wsaa_auth.expira ? st.wsaa_auth.expira.replace('T', ' ').slice(0, 16) : '';
      set('stAuth', true, `OK · expira ${exp}`, '');
    } else {
      const err = st.wsaa_auth?.error || 'Error';
      const short = err.includes('Zero length') ? 'Error interno AFIP' :
                    err.includes('timeout') ? 'Timeout' :
                    err.length > 50 ? err.slice(0, 50) + '...' : err;
      set('stAuth', false, '', short);
    }

    set('stCert', st.cert, 'Cargado', 'No encontrado');
    set('stKey', st.key, 'Cargada', 'No encontrada');
  } catch (e) {
    ids.forEach(id => {
      const el = document.getElementById(id);
      el.className = 'pill pill--err';
      el.textContent = 'Error de conexión';
    });
  }
}

async function resetDB(tablas) {
  const msgs = {
    facturas: 'todas las facturas',
    clientes: 'todos los clientes',
    all: 'todas las facturas y clientes',
  };
  if (!confirm(`¿Estás seguro de que querés borrar ${msgs[tablas]}?\n\nEsta acción no se puede deshacer.`)) return;
  try {
    const res = await fetch(`${API}/admin/reset-db?tablas=${tablas}`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).detail || 'Error');
    toast(`Base de datos inicializada (${msgs[tablas]}).`);
    cargarHistorial();
    cargarClientes();
  } catch (e) {
    toast(`Error: ${e.message}`, 'error');
  }
}

async function subirLogo() {
  const file = document.getElementById('cfgLogoFile').files[0];
  if (!file) { toast('Elegí un archivo primero.', 'warn'); return; }
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch(`${API}/config/logo`, { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || 'Error');
    toast('Logo subido.');
    cargarConfig();
  } catch (e) {
    toast(`No se pudo subir el logo: ${e.message}`, 'error');
  }
}

async function borrarLogo() {
  try {
    const res = await fetch(`${API}/config/logo`, { method: 'DELETE' });
    if (!res.ok) throw new Error();
    toast('Logo eliminado.');
    cargarConfig();
  } catch (_) {
    toast('No se pudo eliminar el logo.', 'error');
  }
}

async function enviarFacturaPorWhatsapp(id) {
  try {
    const res = await fetch(`${API}/facturas/${id}`);
    if (!res.ok) throw new Error(`Error ${res.status}`);
    const f = await res.json();

    const tel = prompt(
      'Número de WhatsApp (con código de país, sin espacios ni +):\n' +
      'Ej: 5491122334455',
      ''
    );
    if (!tel) return;
    const telLimpio = tel.replace(/\D/g,'');
    if (telLimpio.length < 8) { toast('Número inválido.', 'warn'); return; }

    const nro   = fmtNro(f.punto_venta, f.nro_cbte);
    const total = fmt(f.imp_total);
    const nombre = f.receptor_nombre || 'cliente';
    const texto =
      `Hola ${nombre}, te paso la Factura C ${nro} por ${total}. ` +
      `CAE: ${f.cae || '—'}. ` +
      `Te adjunto el PDF en un momento.`;

    // Disparar descarga del PDF para que el usuario lo adjunte manualmente
    descargarPDFById(id);

    const url = `https://wa.me/${telLimpio}?text=${encodeURIComponent(texto)}`;
    window.open(url, '_blank');
    toast('PDF descargado. Adjuntalo en la ventana de WhatsApp.');
  } catch (e) {
    toast(`No se pudo preparar el envío: ${e.message}`, 'error');
  }
}

async function enviarFacturaPorEmail(id, emailSugerido) {
  const to = prompt('Enviar factura a:', emailSugerido || '');
  if (!to) return;
  try {
    const res = await fetch(`${API}/facturas/${id}/email`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || `Error ${res.status}`);
    toast(`Factura enviada a ${to}`);
  } catch (e) {
    toast(`No se pudo enviar: ${e.message}`, 'error');
  }
}

// ═══════════════════════════════════════════
// Reset
// ═══════════════════════════════════════════
function resetForm() {
  ['receptorNombre','nroDoc','receptorEmail','receptorDom','observaciones']
    .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  document.getElementById('condVenta').value = 'Contado';
  document.getElementById('itemsContainer').innerHTML = '';
  state.items = [];
  addItem();
  recalc();
  setHoy();
}

// ═══════════════════════════════════════════
// Helpers de overlay
// ═══════════════════════════════════════════
function openOverlay(id)  { document.getElementById(id).classList.add('open'); }
function closeOverlay(id) { document.getElementById(id).classList.remove('open'); }

// ═══════════════════════════════════════════
// Toast notifications
// ═══════════════════════════════════════════
function toast(msg, type = 'ok') {
  const t = document.createElement('div');
  Object.assign(t.style, {
    position: 'fixed', bottom: '24px', right: '24px', zIndex: 9999,
    background: type === 'error' ? '#c0392b' : type === 'warn' ? '#c9753a' : '#2d6a4f',
    color: '#fff', padding: '.7rem 2.2rem .7rem 1.2rem', borderRadius: '8px',
    fontSize: '.83rem', fontFamily: 'Mulish, sans-serif', fontWeight: '600',
    boxShadow: '0 4px 16px rgba(0,0,0,.18)',
    animation: 'slideUp .22s ease',
    maxWidth: '420px', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    cursor: 'default', userSelect: 'text'
  });

  const text = document.createElement('span');
  text.textContent = msg;
  t.appendChild(text);

  const isPersistent = type === 'error' || type === 'warn';
  if (isPersistent) {
    const close = document.createElement('button');
    close.textContent = '×';
    Object.assign(close.style, {
      position: 'absolute', top: '4px', right: '8px',
      background: 'transparent', border: 'none', color: '#fff',
      fontSize: '1.1rem', lineHeight: '1', cursor: 'pointer',
      fontWeight: '700', padding: '2px 6px'
    });
    close.onclick = () => t.remove();
    t.appendChild(close);
  } else {
    setTimeout(() => t.remove(), 3500);
  }

  document.body.appendChild(t);
}

// ═══════════════════════════════════════════
// Formatters
// ═══════════════════════════════════════════
function fmt(n) {
  return '$ ' + Number(n).toLocaleString('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtDoc(nro, tipoDoc) {
  const s = String(nro).replace(/\D/g, '');
  if (!s || s === '0') return '—';
  const td = Number(tipoDoc);
  // CUIT (80) o CUIL (86): formato XX-XX.XXX.XXX/X  (11 dígitos)
  if ((td === 80 || td === 86) && s.length === 11) {
    return s.slice(0,2) + '-' + s.slice(2,4) + '.' + s.slice(4,7) + '.' + s.slice(7,10) + '/' + s.slice(10);
  }
  // DNI (96): formato XX.XXX.XXX
  if (td === 96) {
    return s.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  }
  return s;
}
function fmtNro(pv, nro) {
  return String(pv).padStart(4,'0') + '-' + String(nro).padStart(8,'0');
}
function fmtFecha(s) {
  if (!s) return '—';
  // Acepta YYYYMMDD o YYYY-MM-DD
  const str = String(s).replace(/-/g,'');
  if (str.length === 8) {
    return `${str.slice(6,8)}/${str.slice(4,6)}/${str.slice(0,4)}`;
  }
  return s;
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
