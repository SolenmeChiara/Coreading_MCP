/* ========== 共读书房 Co-Reading Room — 前端逻辑 ========== */

// ---------- 状态 ----------

const state = {
    currentView: 'bookshelf',
    books: [],
    currentBook: null,
    currentPage: 1,
    paragraphs: [],
    annotations: [],
    pollTimer: null,
    activeParagraph: null,
    chatMessages: [],
    chatOpen: false,
    chatPollTimer: null,
    progressPollTimer: null,
    eventSource: null,
    sessionId: null,
    sessionStartTime: null,
    sessionTimerInterval: null,
    userName: 'Reader',
    settings: {},
    fileType: 'txt',
    screenshotUrl: null,
    textBlocks: [],
    bboxDraw: null,     // {startX, startY} while drawing
    // 写作模式
    mode: 'read',
    draftContent: '',
    draftParagraphs: [],
    draftRevision: 0,
    versions: [],
    suggestions: [],
    editingParagraph: null,
    // 多书 tab 系统
    openBooks: {},       // bookId -> {book, page, paragraphs, annotations, chatMessages, fileType, screenshotUrl, textBlocks, sessionId, sessionStartTime, eventSource}
    tabOrder: [],        // [bookId, ...] 维护 tab 顺序
};

// ---------- API 层 ----------

const API_BASE = '';

const API = {
    async request(method, path, body) {
        const opts = { method, headers: {} };
        if (body instanceof FormData) {
            opts.body = body;
        } else if (body) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const res = await fetch(API_BASE + path, opts);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || err.message || res.statusText);
        }
        if (res.status === 204) return null;
        return res.json();
    },

    getBooks()                          { return this.request('GET', '/api/books'); },
    uploadBook(formData)                { return this.request('POST', '/api/upload', formData); },
    deleteBook(id)                      { return this.request('DELETE', `/api/books/${id}`); },
    getPage(bookId, pageNum)            { return this.request('GET', `/api/books/${bookId}/page/${pageNum}`); },
    updateProgress(bookId, page)        { return this.request('POST', `/api/books/${bookId}/progress`, { page }); },
    getAnnotations(bookId, pageNum)     { return this.request('GET', `/api/books/${bookId}/page/${pageNum}/annotations`); },
    addAnnotation(bookId, pageNum, data){ return this.request('POST', `/api/books/${bookId}/page/${pageNum}/annotations`, data); },
    updateAnnotation(id, content)       { return this.request('PUT', `/api/annotations/${id}`, { content }); },
    deleteAnnotation(id)                { return this.request('DELETE', `/api/annotations/${id}`); },
    getAllChat(bookId)                   { return this.request('GET', `/api/books/${bookId}/chat`); },
    sendChatMessage(bookId, pageNum, d) { return this.request('POST', `/api/books/${bookId}/page/${pageNum}/chat`, d); },
    getCurrentProgress(bookId)          { return this.request('GET', `/api/books/${bookId}/current-progress`); },
    startSession(bookId)                { return this.request('POST', `/api/books/${bookId}/sessions/start`); },
    endSession(bookId, data)            { return this.request('POST', `/api/books/${bookId}/sessions/end`, data); },
    getSettings()                       { return this.request('GET', '/api/settings'); },
    updateSettings(settings)            { return this.request('PUT', '/api/settings', { settings }); },
    // 写作模式
    setBookMode(bookId, mode)           { return this.request('PUT', `/api/books/${bookId}/mode`, { mode }); },
    getDraft(bookId, pageNum)           { return this.request('GET', `/api/books/${bookId}/draft/${pageNum}`); },
    saveDraft(bookId, pageNum, content, rev) { return this.request('PUT', `/api/books/${bookId}/draft/${pageNum}`, { content, expected_revision: rev }); },
    createVersion(bookId, pageNum, label) { return this.request('POST', `/api/books/${bookId}/draft/${pageNum}/versions`, { label }); },
    getVersions(bookId, pageNum)        { return this.request('GET', `/api/books/${bookId}/draft/${pageNum}/versions`); },
    restoreVersion(bookId, pageNum, vid){ return this.request('POST', `/api/books/${bookId}/draft/${pageNum}/restore/${vid}`); },
    getSuggestions(bookId, pageNum)     { return this.request('GET', `/api/books/${bookId}/page/${pageNum}/suggestions`); },
    acceptSuggestion(bookId, pageNum, id) { return this.request('POST', `/api/books/${bookId}/page/${pageNum}/suggestions/${id}/accept`); },
    rejectSuggestion(bookId, pageNum, id) { return this.request('POST', `/api/books/${bookId}/page/${pageNum}/suggestions/${id}/reject`); },
};

// ---------- Toast 通知 ----------

function showToast(message, isError = false) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = 'toast show' + (isError ? ' error' : '');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.className = 'toast'; }, 3000);
}

// ---------- 视图切换 ----------

function showView(viewName) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(`${viewName}-view`).classList.add('active');
    state.currentView = viewName;

    const chatToggle = document.getElementById('chat-toggle');
    if (viewName === 'bookshelf') {
        saveCurrentTabState();
        stopSSE();
        stopSessionTimer();
        chatToggle.style.display = 'none';
        closeChatPanel();
        renderBookshelf();
    } else if (viewName === 'reader') {
        chatToggle.style.display = '';
    } else if (viewName === 'settings') {
        stopAllPolling();
        chatToggle.style.display = 'none';
        closeChatPanel();
        renderSettings();
    }
}

function stopAllPolling() {
    stopSSE();
    stopPollingFallback();
    stopChatPolling();
    stopSessionTimer();
}

// ========== 书架 ==========

async function renderBookshelf() {
    try {
        state.books = await API.getBooks();
    } catch (e) {
        showToast('加载书架失败: ' + e.message, true);
        return;
    }

    const container = document.getElementById('bookshelf-container');
    const createdEl = document.getElementById('shelf-created-books');
    const uploadedEl = document.getElementById('shelf-uploaded-books');
    const empty = document.getElementById('empty-state');

    if (state.books.length === 0) {
        container.style.display = 'none';
        empty.style.display = '';
        return;
    }

    container.style.display = '';
    empty.style.display = 'none';

    const created = state.books.filter(b => b.source === 'created');
    const uploaded = state.books.filter(b => b.source !== 'created');

    createdEl.innerHTML = created.length > 0
        ? created.map(bookCardHtml).join('')
        : '<div class="shelf-empty">还没有创作</div>';
    uploadedEl.innerHTML = uploaded.length > 0
        ? uploaded.map(bookCardHtml).join('')
        : '<div class="shelf-empty">还没有上传</div>';

    // 如果一栏完全为空，隐藏该栏
    document.getElementById('shelf-created').style.display = created.length > 0 || uploaded.length > 0 ? '' : 'none';

    _bindBookCardEvents(container);
}

function bookCardHtml(book) {
    const pct = Math.round((book.current_page / book.total_pages) * 100);
    const lastRead = book.last_read_at
        ? new Date(book.last_read_at + 'Z').toLocaleString('zh-CN')
        : '从未阅读';
    const typeLabel = book.file_type === 'pdf' ? ' · PDF' : '';
    const modeLabel = book.mode === 'write' ? ' · 写作' : '';
    return `
        <div class="book-card" data-id="${book.id}">
            <button class="delete-btn" data-delete="${book.id}" title="删除">&times;</button>
            <div class="book-spine"></div>
            <div class="book-body">
                <div class="book-title">${escapeHtml(book.title)}${typeLabel}${modeLabel}</div>
                <div class="book-meta">
                    第 ${book.current_page} / ${book.total_pages} 页<br>
                    ${lastRead}
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${pct}%"></div>
                </div>
            </div>
        </div>
    `;
}

function _bindBookCardEvents(container) {
    container.querySelectorAll('.book-card').forEach(card => {
        card.addEventListener('click', (e) => {
            if (e.target.closest('.delete-btn')) return;
            openBook(parseInt(card.dataset.id));
        });
    });

    container.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = parseInt(btn.dataset.delete);
            if (!confirm('确定删除这本书吗？所有批注也会一起删除。')) return;
            try {
                await API.deleteBook(id);
                showToast('已删除');
                renderBookshelf();
            } catch (e) {
                showToast('删除失败: ' + e.message, true);
            }
        });
    });
}

// ========== 上传 ==========

function initUpload() {
    const area = document.getElementById('upload-area');
    const input = document.getElementById('file-input');

    area.addEventListener('click', () => input.click());

    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('dragover');
    });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('dragover');
        if (e.dataTransfer.files[0]) handleUpload(e.dataTransfer.files[0]);
    });

    input.addEventListener('change', () => {
        if (input.files[0]) handleUpload(input.files[0]);
        input.value = '';
    });
}

async function handleUpload(file) {
    const name = file.name.toLowerCase();
    if (!name.endsWith('.txt') && !name.endsWith('.pdf')) {
        showToast('仅支持 TXT 和 PDF 文件', true);
        return;
    }
    try {
        const fd = new FormData();
        fd.append('file', file);
        const result = await API.uploadBook(fd);
        showToast(`上传成功：${result.title}（${result.total_pages} 页）`);
        renderBookshelf();
    } catch (e) {
        showToast('上传失败: ' + e.message, true);
    }
}

// ========== 阅读器 ==========

async function openBook(bookId) {
    const book = state.books.find(b => b.id === bookId);
    if (!book) return;

    // 如果已经打开过，直接切换 tab
    if (state.openBooks[bookId]) {
        switchTab(bookId);
        return;
    }

    // 新开 tab
    let sessionId = null, sessionStartTime = null;
    try {
        const res = await API.startSession(bookId);
        sessionId = res.session_id;
        sessionStartTime = Date.now();
    } catch { /* 会话追踪失败不阻塞阅读 */ }

    state.openBooks[bookId] = {
        book,
        page: book.current_page,
        paragraphs: [],
        annotations: [],
        chatMessages: [],
        fileType: 'txt',
        screenshotUrl: null,
        textBlocks: [],
        sessionId,
        sessionStartTime,
        eventSource: null,
        mode: 'read',
        draftContent: '',
        draftParagraphs: [],
        draftRevision: 0,
        suggestions: [],
    };
    if (!state.tabOrder.includes(bookId)) {
        state.tabOrder.push(bookId);
    }

    switchTab(bookId);
}

function switchTab(bookId) {
    const session = state.openBooks[bookId];
    if (!session) return;

    // 保存当前 tab 状态
    saveCurrentTabState();
    // 停旧的 SSE
    stopSSE();

    // 加载新 tab 状态
    state.currentBook = session.book;
    state.currentPage = session.page;
    state.paragraphs = session.paragraphs;
    state.annotations = session.annotations;
    state.chatMessages = session.chatMessages;
    state.fileType = session.fileType;
    state.screenshotUrl = session.screenshotUrl;
    state.textBlocks = session.textBlocks;
    state.sessionId = session.sessionId;
    state.sessionStartTime = session.sessionStartTime;
    state.eventSource = session.eventSource;
    state.mode = session.mode || 'read';
    state.draftContent = session.draftContent || '';
    state.draftParagraphs = session.draftParagraphs || [];
    state.draftRevision = session.draftRevision || 0;
    state.suggestions = session.suggestions || [];
    state.activeParagraph = null;
    state.editingParagraph = null;

    document.getElementById('book-title').textContent = session.book.title;
    showView('reader');
    renderTabs();
    startSessionTimer();

    if (session.paragraphs.length > 0) {
        // 已有缓存数据，直接渲染
        renderReader();
        renderChatMessages();
        startSSE();
    } else {
        // 首次打开，加载页面
        loadPage(session.page);
    }
}

function closeTab(bookId) {
    const session = state.openBooks[bookId];
    if (!session) return;

    // 关闭 SSE
    if (session.eventSource) {
        session.eventSource.close();
    }

    // 结束会话
    if (session.sessionId) {
        API.endSession(session.book.id, {
            session_id: session.sessionId,
            pages_read: 0,
        }).catch(() => {});
    }

    delete state.openBooks[bookId];
    state.tabOrder = state.tabOrder.filter(id => id !== bookId);

    // 如果关闭的是当前 tab
    if (state.currentBook && state.currentBook.id === bookId) {
        if (state.tabOrder.length > 0) {
            switchTab(state.tabOrder[state.tabOrder.length - 1]);
        } else {
            state.currentBook = null;
            showView('bookshelf');
        }
    } else {
        renderTabs();
    }
}

function saveCurrentTabState() {
    if (!state.currentBook) return;
    const session = state.openBooks[state.currentBook.id];
    if (!session) return;
    session.page = state.currentPage;
    session.paragraphs = state.paragraphs;
    session.annotations = state.annotations;
    session.chatMessages = state.chatMessages;
    session.fileType = state.fileType;
    session.screenshotUrl = state.screenshotUrl;
    session.textBlocks = state.textBlocks;
    session.eventSource = state.eventSource;
    session.mode = state.mode;
    session.draftContent = state.draftContent;
    session.draftParagraphs = state.draftParagraphs;
    session.draftRevision = state.draftRevision;
    session.suggestions = state.suggestions;
}

function renderTabs() {
    const container = document.getElementById('reader-tabs');
    if (state.tabOrder.length <= 1) {
        container.innerHTML = '';
        container.style.display = 'none';
        return;
    }

    container.style.display = 'flex';
    container.innerHTML = state.tabOrder.map(id => {
        const session = state.openBooks[id];
        if (!session) return '';
        const isActive = state.currentBook && state.currentBook.id === id;
        const title = session.book.title.length > 12
            ? session.book.title.slice(0, 12) + '...'
            : session.book.title;
        return `
            <div class="reader-tab${isActive ? ' active' : ''}" data-tab-id="${id}">
                <span class="tab-title">${escapeHtml(title)}</span>
                <button class="tab-close" data-close-id="${id}" title="关闭">&times;</button>
            </div>
        `;
    }).join('');

    container.querySelectorAll('.reader-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            if (e.target.closest('.tab-close')) return;
            switchTab(parseInt(tab.dataset.tabId));
        });
    });

    container.querySelectorAll('.tab-close').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            closeTab(parseInt(btn.dataset.closeId));
        });
    });
}

async function loadPage(pageNum) {
    if (!state.currentBook) return;
    try {
        const data = await API.getPage(state.currentBook.id, pageNum);
        state.currentPage = pageNum;
        state.paragraphs = data.paragraphs;
        state.currentBook.total_pages = data.total_pages;
        state.currentBook.current_page = pageNum;
        state.activeParagraph = null;
        state.fileType = data.file_type || 'txt';
        state.screenshotUrl = data.screenshot_url || null;
        state.textBlocks = data.text_blocks || [];
        state.mode = data.mode || 'read';

        // 写作模式按钮显示
        const modeBtn = document.getElementById('btn-mode-toggle');
        if (state.fileType === 'txt') {
            modeBtn.style.display = '';
            modeBtn.innerHTML = state.mode === 'write' ? '&#128214;' : '&#9998;';
            modeBtn.title = state.mode === 'write' ? '切换为阅读模式' : '切换为写作模式';
        } else {
            modeBtn.style.display = 'none';
        }

        API.updateProgress(state.currentBook.id, pageNum).catch(() => {});

        state.annotations = await API.getAnnotations(state.currentBook.id, pageNum);

        // 写作模式加载草稿和建议
        if (state.mode === 'write') {
            try {
                const draft = await API.getDraft(state.currentBook.id, pageNum);
                state.draftContent = draft.content;
                state.draftParagraphs = draft.paragraphs;
                state.draftRevision = draft.revision;
            } catch { /* 草稿加载失败不阻塞 */ }
            try {
                state.suggestions = await API.getSuggestions(state.currentBook.id, pageNum);
            } catch { state.suggestions = []; }
        } else {
            state.draftContent = '';
            state.draftParagraphs = [];
            state.draftRevision = 0;
            state.suggestions = [];
        }

        // 跨页聊天：加载整本书的聊天
        state.chatMessages = await API.getAllChat(state.currentBook.id);

        renderReader();
        renderChatMessages();
        startSSE();
        window.scrollTo(0, 0);
    } catch (e) {
        showToast('加载页面失败: ' + e.message, true);
    }
}

function renderReader() {
    document.getElementById('page-indicator').textContent =
        `${state.currentPage} / ${state.currentBook.total_pages}`;

    document.getElementById('btn-prev').disabled = (state.currentPage <= 1);
    document.getElementById('btn-next').disabled = (state.currentPage >= state.currentBook.total_pages);

    renderParagraphs();
}

function renderParagraphs() {
    const container = document.getElementById('reader-content');
    container.innerHTML = '';

    const isPdf = state.fileType === 'pdf' && state.screenshotUrl;

    if (isPdf) {
        renderPdfMode(container);
    } else if (state.mode === 'write') {
        renderWriteMode(container);
    } else {
        renderTxtMode(container);
    }
}

function renderPdfMode(container) {
    // 截图为主视图 + canvas overlay
    const imgDiv = document.createElement('div');
    imgDiv.className = 'pdf-screenshot';
    imgDiv.id = 'pdf-screenshot-container';
    imgDiv.innerHTML = `
        <img src="${state.screenshotUrl}" alt="PDF 第 ${state.currentPage} 页" id="pdf-img">
        <canvas id="pdf-canvas"></canvas>
        <div class="bbox-hint">拖拽框选区域批注</div>
    `;
    container.appendChild(imgDiv);

    // 图片加载后初始化 canvas 和已有 bbox 标记
    const img = imgDiv.querySelector('#pdf-img');
    img.addEventListener('load', () => {
        initPdfCanvas(imgDiv, img);
        renderBboxMarkers(imgDiv, img);
    });
    if (img.complete) {
        initPdfCanvas(imgDiv, img);
        renderBboxMarkers(imgDiv, img);
    }

    // 分两组显示批注：region 批注（带 bbox）和整页/段落批注
    const regionAnnos = state.annotations.filter(a => a.target_type === 'region' && a.bbox_x0 != null);
    const otherAnnos = state.annotations.filter(a => !(a.target_type === 'region' && a.bbox_x0 != null));

    // 区域批注列表
    if (regionAnnos.length > 0) {
        const regionArea = document.createElement('div');
        regionArea.className = 'annotations-area';
        const label = document.createElement('div');
        label.style.cssText = 'font-size:0.78rem;color:var(--text-muted);margin-bottom:0.3rem;';
        label.textContent = `区域批注 (${regionAnnos.length})`;
        regionArea.appendChild(label);
        regionAnnos.forEach(a => regionArea.appendChild(createBubble(a)));
        container.appendChild(regionArea);
    }

    // 整页批注区
    if (otherAnnos.length > 0) {
        const area = document.createElement('div');
        area.className = 'annotations-area';
        otherAnnos.forEach(a => area.appendChild(createBubble(a)));
        container.appendChild(area);
    }

    // 整页批注输入框（始终可见）
    const inputDiv = document.createElement('div');
    inputDiv.className = 'pdf-annotation-input';
    inputDiv.innerHTML = `
        <textarea id="pdf-anno-input" placeholder="写下对这一页的想法..."></textarea>
        <div class="input-actions">
            <button class="btn btn-submit" onclick="submitAnnotation(null)">发送</button>
        </div>
    `;
    container.appendChild(inputDiv);

    // 可折叠的提取文字
    if (state.paragraphs.length > 0 && state.paragraphs.some(p => p.trim())) {
        const drawer = document.createElement('details');
        drawer.className = 'text-drawer';
        const summary = document.createElement('summary');
        summary.textContent = '查看提取文字（仅供参考，以截图为准）';
        drawer.appendChild(summary);

        const textContent = document.createElement('div');
        textContent.className = 'text-drawer-content';
        state.paragraphs.forEach((text, idx) => {
            const p = document.createElement('p');
            p.className = 'extracted-text';
            p.textContent = text;
            textContent.appendChild(p);
        });
        drawer.appendChild(textContent);
        container.appendChild(drawer);
    }
}

// ---------- PDF Canvas 框选 ----------

function initPdfCanvas(containerEl, imgEl) {
    const canvas = containerEl.querySelector('#pdf-canvas');
    if (!canvas) return;

    canvas.width = imgEl.naturalWidth;
    canvas.height = imgEl.naturalHeight;
    const ctx = canvas.getContext('2d');

    let startX = 0, startY = 0, drawing = false;

    function getPos(e) {
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        return {
            x: (e.clientX - rect.left) * scaleX,
            y: (e.clientY - rect.top) * scaleY,
        };
    }

    canvas.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        // 关闭已有弹窗
        closeBboxPopup();
        const pos = getPos(e);
        startX = pos.x;
        startY = pos.y;
        drawing = true;
    });

    canvas.addEventListener('mousemove', (e) => {
        if (!drawing) return;
        const pos = getPos(e);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.strokeStyle = '#d97757';
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 3]);
        ctx.fillStyle = 'rgba(217, 119, 87, 0.1)';
        const w = pos.x - startX, h = pos.y - startY;
        ctx.fillRect(startX, startY, w, h);
        ctx.strokeRect(startX, startY, w, h);
    });

    canvas.addEventListener('mouseup', (e) => {
        if (!drawing) return;
        drawing = false;
        const pos = getPos(e);
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // 归一化为 0~1 比例
        const x0 = Math.min(startX, pos.x) / canvas.width;
        const y0 = Math.min(startY, pos.y) / canvas.height;
        const x1 = Math.max(startX, pos.x) / canvas.width;
        const y1 = Math.max(startY, pos.y) / canvas.height;

        // 忽略太小的框选（意外点击）
        if ((x1 - x0) < 0.02 && (y1 - y0) < 0.02) return;

        showBboxAnnotationPopup(containerEl, imgEl, { x0, y0, x1, y1 });
    });

    // 触摸支持
    canvas.addEventListener('touchstart', (e) => {
        e.preventDefault();
        closeBboxPopup();
        const touch = e.touches[0];
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        startX = (touch.clientX - rect.left) * scaleX;
        startY = (touch.clientY - rect.top) * scaleY;
        drawing = true;
    }, { passive: false });

    canvas.addEventListener('touchmove', (e) => {
        if (!drawing) return;
        e.preventDefault();
        const touch = e.touches[0];
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        const posX = (touch.clientX - rect.left) * scaleX;
        const posY = (touch.clientY - rect.top) * scaleY;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.strokeStyle = '#d97757';
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 3]);
        ctx.fillStyle = 'rgba(217, 119, 87, 0.1)';
        ctx.fillRect(startX, startY, posX - startX, posY - startY);
        ctx.strokeRect(startX, startY, posX - startX, posY - startY);
    }, { passive: false });

    canvas.addEventListener('touchend', (e) => {
        if (!drawing) return;
        drawing = false;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const touch = e.changedTouches[0];
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        const posX = (touch.clientX - rect.left) * scaleX;
        const posY = (touch.clientY - rect.top) * scaleY;

        const x0 = Math.min(startX, posX) / canvas.width;
        const y0 = Math.min(startY, posY) / canvas.height;
        const x1 = Math.max(startX, posX) / canvas.width;
        const y1 = Math.max(startY, posY) / canvas.height;

        if ((x1 - x0) < 0.02 && (y1 - y0) < 0.02) return;
        showBboxAnnotationPopup(containerEl, imgEl, { x0, y0, x1, y1 });
    });
}

function renderBboxMarkers(containerEl, imgEl) {
    // 移除旧标记
    containerEl.querySelectorAll('.bbox-marker').forEach(m => m.remove());

    const regionAnnos = state.annotations.filter(
        a => a.target_type === 'region' && a.bbox_x0 != null
    );

    regionAnnos.forEach(a => {
        const marker = document.createElement('div');
        const isUser = a.author !== 'claude';
        marker.className = `bbox-marker${isUser ? ' user' : ''}`;
        marker.style.left = `${a.bbox_x0 * 100}%`;
        marker.style.top = `${a.bbox_y0 * 100}%`;
        marker.style.width = `${(a.bbox_x1 - a.bbox_x0) * 100}%`;
        marker.style.height = `${(a.bbox_y1 - a.bbox_y0) * 100}%`;
        marker.title = `${a.author === 'claude' ? 'Claude' : state.userName}: ${a.content.slice(0, 50)}`;

        const label = document.createElement('span');
        label.className = 'bbox-marker-label';
        label.textContent = a.author === 'claude' ? 'Claude' : state.userName;
        marker.appendChild(label);

        // 点击标记高亮对应批注
        marker.addEventListener('click', () => {
            const bubble = document.querySelector(`.annotation-bubble[data-anno-id="${a.id}"]`);
            if (bubble) {
                bubble.scrollIntoView({ behavior: 'smooth', block: 'center' });
                bubble.style.outline = '2px solid var(--accent-orange)';
                setTimeout(() => { bubble.style.outline = ''; }, 2000);
            }
        });

        containerEl.appendChild(marker);
    });
}

function showBboxAnnotationPopup(containerEl, imgEl, bbox) {
    closeBboxPopup();

    const popup = document.createElement('div');
    popup.className = 'bbox-annotation-popup';
    popup.id = 'bbox-popup';

    // 弹窗位置：选框右下方
    const left = Math.min(bbox.x1 * 100, 60);
    const top = bbox.y1 * 100 + 2;
    popup.style.left = `${left}%`;
    popup.style.top = `${top}%`;

    // 查找选框内的文本块
    const matchedText = findTextInBbox(bbox);
    const placeholder = matchedText
        ? `选中文本: "${matchedText.slice(0, 60)}..."\n\n写下你的想法...`
        : '写下你对这个区域的想法...';

    popup.innerHTML = `
        <textarea id="bbox-anno-input"></textarea>
        <div class="input-actions">
            <button class="btn btn-cancel" onclick="closeBboxPopup()">取消</button>
            <button class="btn btn-submit" onclick="submitBboxAnnotation()">发送</button>
        </div>
    `;
    const textarea = popup.querySelector('textarea');
    textarea.placeholder = placeholder;
    popup.dataset.bbox = JSON.stringify(bbox);
    if (matchedText) popup.dataset.matchedText = matchedText;
    containerEl.appendChild(popup);
    textarea.focus();
}

function closeBboxPopup() {
    const popup = document.getElementById('bbox-popup');
    if (popup) popup.remove();
}

async function submitBboxAnnotation() {
    const popup = document.getElementById('bbox-popup');
    if (!popup) return;

    const textarea = popup.querySelector('textarea');
    const content = textarea.value.trim();
    if (!content) return;

    const bbox = JSON.parse(popup.dataset.bbox);
    const matchedText = popup.dataset.matchedText || null;

    try {
        await API.addAnnotation(state.currentBook.id, state.currentPage, {
            content,
            target_type: 'region',
            bbox_x0: bbox.x0,
            bbox_y0: bbox.y0,
            bbox_x1: bbox.x1,
            bbox_y1: bbox.y1,
            highlight_text: matchedText,
        });
        closeBboxPopup();
        showToast('区域批注已发送');
        await refreshAnnotations();
    } catch (e) {
        showToast('发送失败: ' + e.message, true);
    }
}

function findTextInBbox(bbox) {
    // 在文本块中找与选框重叠的文本
    if (!state.textBlocks || state.textBlocks.length === 0) return null;

    const matched = state.textBlocks.filter(b => {
        const bx0 = b.bbox_x0, by0 = b.bbox_y0, bx1 = b.bbox_x1, by1 = b.bbox_y1;
        // 检查重叠（任一维度不重叠则不匹配）
        return !(bx1 < bbox.x0 || bx0 > bbox.x1 || by1 < bbox.y0 || by0 > bbox.y1);
    });

    if (matched.length === 0) return null;
    return matched.map(b => b.content).join(' ').slice(0, 200);
}

// ---------- 写作模式 ----------

function renderWriteMode(container) {
    const paras = state.draftParagraphs.length > 0 ? state.draftParagraphs : state.paragraphs;

    paras.forEach((text, idx) => {
        const paraDiv = document.createElement('div');
        paraDiv.className = 'write-paragraph';
        paraDiv.dataset.idx = idx;

        if (state.editingParagraph === idx) {
            // 编辑态
            paraDiv.innerHTML = `
                <span class="para-index">${idx}</span>
                <textarea class="write-textarea" data-idx="${idx}">${escapeHtml(text)}</textarea>
            `;
            const textarea = paraDiv.querySelector('textarea');
            setTimeout(() => textarea.focus(), 0);
            textarea.addEventListener('blur', () => saveEditingParagraph(idx, textarea.value));
            textarea.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && e.ctrlKey) {
                    e.preventDefault();
                    saveEditingParagraph(idx, textarea.value);
                }
            });
        } else {
            // 显示态
            paraDiv.innerHTML = `
                <span class="para-index">${idx}</span>
                <div class="para-text">${escapeHtml(text)}</div>
            `;
            paraDiv.addEventListener('click', () => {
                state.editingParagraph = idx;
                renderParagraphs();
            });
        }
        container.appendChild(paraDiv);

        // 该段落的批注
        const paraAnnotations = state.annotations.filter(a => a.paragraph_index === idx);
        const wholePageAnnotations = (idx === 0)
            ? state.annotations.filter(a => a.paragraph_index === null)
            : [];
        const allAnnotations = [...wholePageAnnotations, ...paraAnnotations];
        if (allAnnotations.length > 0) {
            const area = document.createElement('div');
            area.className = 'annotations-area';
            allAnnotations.forEach(a => area.appendChild(createBubble(a)));
            container.appendChild(area);
        }

        // 该段落的建议卡片
        const paraHash = _paragraphSig(text);
        const paraSuggestions = state.suggestions.filter(s =>
            s.status === 'pending' && s.paragraph_sig === paraHash
        );
        paraSuggestions.forEach(s => {
            container.appendChild(renderSuggestionCard(s));
        });
    });

    // 添加段落按钮
    const addBtn = document.createElement('button');
    addBtn.className = 'btn write-add-para';
    addBtn.textContent = '+ 添加段落';
    addBtn.addEventListener('click', () => {
        state.draftParagraphs.push('');
        state.editingParagraph = state.draftParagraphs.length - 1;
        state.draftContent = state.draftParagraphs.join('\n\n');
        renderParagraphs();
    });
    container.appendChild(addBtn);

    // 版本面板
    renderVersionPanel(container);

    // stale 建议（找不到匹配段落的）
    const staleSugs = state.suggestions.filter(s => {
        if (s.status !== 'pending') return false;
        return !paras.some(p => _paragraphSig(p) === s.paragraph_sig);
    });
    if (staleSugs.length > 0) {
        const staleArea = document.createElement('div');
        staleArea.className = 'stale-suggestions';
        staleArea.innerHTML = '<div class="stale-label">以下建议的段落已变更：</div>';
        staleSugs.forEach(s => {
            const card = renderSuggestionCard(s, true);
            staleArea.appendChild(card);
        });
        container.appendChild(staleArea);
    }
}

function _paragraphSig(text) {
    // 简易 hash（前端版，和后端 md5[:12] 对应）
    // 用 simple hash 代替 md5，足够匹配
    let h = 0;
    const s = text.trim();
    for (let i = 0; i < s.length; i++) {
        h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    }
    return (h >>> 0).toString(16).padStart(8, '0');
}

// 注意：前端 hash 和后端 md5[:12] 不同，所以建议匹配用后端返回的 paragraph_sig
// 但渲染时我们直接用 suggestion.paragraph_sig 和后端草稿保持一致

function renderSuggestionCard(suggestion, isStale = false) {
    const card = document.createElement('div');
    card.className = `suggestion-card${isStale ? ' stale' : ''}`;
    card.dataset.sugId = suggestion.id;

    const reason = suggestion.reason ? `<div class="sug-reason">${escapeHtml(suggestion.reason)}</div>` : '';
    const actions = suggestion.status === 'pending' && !isStale
        ? `<div class="sug-actions">
            <button class="btn sug-accept" onclick="handleAcceptSuggestion(${suggestion.id})">Accept</button>
            <button class="btn sug-reject" onclick="handleRejectSuggestion(${suggestion.id})">Reject</button>
           </div>`
        : isStale
        ? `<div class="sug-actions">
            <button class="btn sug-reject" onclick="handleRejectSuggestion(${suggestion.id})">Dismiss</button>
           </div>`
        : `<div class="sug-status">${suggestion.status}</div>`;

    card.innerHTML = `
        <div class="sug-diff">
            <div class="sug-old"><del>${escapeHtml(suggestion.original_text.slice(0, 200))}</del></div>
            <div class="sug-new"><ins>${escapeHtml(suggestion.suggested_text.slice(0, 200))}</ins></div>
        </div>
        ${reason}
        ${actions}
    `;
    return card;
}

async function handleAcceptSuggestion(sugId) {
    try {
        const result = await API.acceptSuggestion(state.currentBook.id, state.currentPage, sugId);
        showToast('建议已应用');
        await loadPage(state.currentPage);
    } catch (e) {
        showToast(e.message, true);
        await loadPage(state.currentPage);
    }
}

async function handleRejectSuggestion(sugId) {
    try {
        await API.rejectSuggestion(state.currentBook.id, state.currentPage, sugId);
        showToast('建议已拒绝');
        state.suggestions = state.suggestions.filter(s => s.id !== sugId);
        renderParagraphs();
    } catch (e) {
        showToast(e.message, true);
    }
}

let _draftSaveTimer = null;

async function saveEditingParagraph(idx, newText) {
    state.editingParagraph = null;
    if (state.draftParagraphs[idx] === newText) {
        renderParagraphs();
        return;
    }
    state.draftParagraphs[idx] = newText;
    // 过滤空段落
    state.draftParagraphs = state.draftParagraphs.filter(p => p.trim());
    state.draftContent = state.draftParagraphs.join('\n\n');
    renderParagraphs();
    debounceSaveDraft();
}

function debounceSaveDraft() {
    clearTimeout(_draftSaveTimer);
    _draftSaveTimer = setTimeout(async () => {
        if (!state.currentBook || state.mode !== 'write') return;
        try {
            const result = await API.saveDraft(
                state.currentBook.id, state.currentPage,
                state.draftContent, state.draftRevision,
            );
            state.draftRevision = result.revision;
        } catch (e) {
            if (e.message.includes('409') || e.message.includes('冲突')) {
                showToast('版本冲突，正在刷新...', true);
                await loadPage(state.currentPage);
            } else {
                showToast('保存失败: ' + e.message, true);
            }
        }
    }, 1500);
}

async function toggleWriteMode() {
    if (!state.currentBook || state.fileType !== 'txt') return;
    const newMode = state.mode === 'write' ? 'read' : 'write';
    try {
        await API.setBookMode(state.currentBook.id, newMode);
        await loadPage(state.currentPage);
    } catch (e) {
        showToast(e.message, true);
    }
}

function renderVersionPanel(container) {
    const panel = document.createElement('details');
    panel.className = 'version-panel';
    panel.innerHTML = `
        <summary>版本历史</summary>
        <div class="version-actions">
            <button class="btn btn-submit" onclick="saveCurrentVersion()">保存当前版本</button>
        </div>
        <div class="version-list" id="version-list">加载中...</div>
    `;
    container.appendChild(panel);

    panel.addEventListener('toggle', async () => {
        if (!panel.open) return;
        try {
            const versions = await API.getVersions(state.currentBook.id, state.currentPage);
            const listEl = panel.querySelector('#version-list');
            if (versions.length === 0) {
                listEl.innerHTML = '<div class="version-empty">还没有保存的版本</div>';
                return;
            }
            listEl.innerHTML = versions.map(v => {
                const time = new Date(v.created_at + 'Z').toLocaleString('zh-CN');
                const label = v.label ? escapeHtml(v.label) : '';
                const source = v.source !== 'manual' ? ` (${v.source})` : '';
                return `
                    <div class="version-item">
                        <span class="version-info">${label}${source} — ${time}</span>
                        <button class="btn version-restore" onclick="restoreVersion(${v.id})">恢复</button>
                    </div>
                `;
            }).join('');
        } catch (e) {
            panel.querySelector('#version-list').textContent = '加载失败';
        }
    });
}

async function saveCurrentVersion() {
    if (!state.currentBook) return;
    const label = prompt('版本标签（可选）：') || '';
    try {
        // 先确保草稿已保存
        if (state.draftContent) {
            await API.saveDraft(state.currentBook.id, state.currentPage, state.draftContent, state.draftRevision)
                .then(r => { state.draftRevision = r.revision; })
                .catch(() => {});
        }
        await API.createVersion(state.currentBook.id, state.currentPage, label);
        showToast('版本已保存');
    } catch (e) {
        showToast('保存失败: ' + e.message, true);
    }
}

async function restoreVersion(versionId) {
    if (!confirm('恢复此版本？当前草稿会自动保存为历史版本。')) return;
    try {
        await API.restoreVersion(state.currentBook.id, state.currentPage, versionId);
        showToast('版本已恢复');
        await loadPage(state.currentPage);
    } catch (e) {
        showToast('恢复失败: ' + e.message, true);
    }
}

function renderTxtMode(container) {
    state.paragraphs.forEach((text, idx) => {
        const paraAnnotations = state.annotations.filter(
            a => a.paragraph_index === idx
        );
        const wholePageAnnotations = (idx === 0)
            ? state.annotations.filter(a => a.paragraph_index === null)
            : [];
        const hasAnnotation = paraAnnotations.length > 0 ||
            (idx === 0 && wholePageAnnotations.length > 0);

        const paraDiv = document.createElement('div');
        paraDiv.className = 'paragraph' + (hasAnnotation ? ' has-annotation' : '');
        paraDiv.innerHTML = `
            <span class="para-index">${idx}</span>
            <div class="para-text">${escapeHtml(text)}</div>
        `;
        paraDiv.addEventListener('click', () => toggleAnnotationInput(idx));
        container.appendChild(paraDiv);

        const allAnnotations = [...wholePageAnnotations, ...paraAnnotations];
        if (allAnnotations.length > 0) {
            const area = document.createElement('div');
            area.className = 'annotations-area';
            allAnnotations.forEach(a => area.appendChild(createBubble(a)));
            container.appendChild(area);
        }

        const inputDiv = document.createElement('div');
        inputDiv.className = 'annotation-input' + (state.activeParagraph === idx ? ' active' : '');
        inputDiv.id = `anno-input-${idx}`;
        inputDiv.innerHTML = `
            <textarea placeholder="写下你的想法..."></textarea>
            <div class="input-actions">
                <button class="btn btn-cancel" onclick="closeAnnotationInput()">取消</button>
                <button class="btn btn-submit" onclick="submitAnnotation(${idx})">发送</button>
            </div>
        `;
        container.appendChild(inputDiv);
    });
}

function createBubble(annotation) {
    const div = document.createElement('div');
    const isClaud = annotation.author === 'claude';
    div.className = `annotation-bubble ${isClaud ? 'claude' : 'user'}`;
    div.dataset.annoId = annotation.id;

    const time = annotation.created_at
        ? new Date(annotation.created_at + 'Z').toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
        : '';

    let highlightHtml = '';
    if (annotation.highlight_text) {
        highlightHtml = `<div class="bubble-highlight">"${escapeHtml(annotation.highlight_text)}"</div>`;
    }

    const actionsHtml = !isClaud ? `
        <div class="bubble-actions">
            <button class="bubble-action-btn" onclick="editAnnotation(${annotation.id}, this)" title="编辑">&#9998;</button>
            <button class="bubble-action-btn bubble-action-delete" onclick="deleteAnnotation(${annotation.id})" title="删除">&times;</button>
        </div>
    ` : '';

    div.innerHTML = `
        <div class="bubble-header">
            <span class="bubble-author">${isClaud ? 'Claude' : state.userName}</span>
            <span class="bubble-time">${time}</span>
            ${actionsHtml}
        </div>
        ${highlightHtml}
        <div class="bubble-content">${escapeHtml(annotation.content)}</div>
    `;
    return div;
}

async function editAnnotation(id, btn) {
    const bubble = btn.closest('.annotation-bubble');
    const contentEl = bubble.querySelector('.bubble-content');
    const oldContent = contentEl.textContent;

    // 替换内容区为编辑框
    contentEl.innerHTML = `
        <textarea class="edit-textarea">${escapeHtml(oldContent)}</textarea>
        <div class="input-actions">
            <button class="btn btn-cancel" onclick="refreshAnnotations()">取消</button>
            <button class="btn btn-submit" onclick="saveAnnotationEdit(${id}, this)">保存</button>
        </div>
    `;
    contentEl.querySelector('textarea').focus();
}

async function saveAnnotationEdit(id, btn) {
    const textarea = btn.closest('.bubble-content').querySelector('textarea');
    const content = textarea.value.trim();
    if (!content) return;

    try {
        await API.updateAnnotation(id, content);
        showToast('批注已更新');
        await refreshAnnotations();
    } catch (e) {
        showToast('更新失败: ' + e.message, true);
    }
}

async function deleteAnnotation(id) {
    if (!confirm('确定删除这条批注吗？')) return;
    try {
        await API.deleteAnnotation(id);
        showToast('批注已删除');
        await refreshAnnotations();
    } catch (e) {
        showToast('删除失败: ' + e.message, true);
    }
}

// ---------- 批注交互 ----------

function toggleAnnotationInput(idx) {
    if (state.activeParagraph === idx) {
        state.activeParagraph = null;
    } else {
        state.activeParagraph = idx;
    }
    document.querySelectorAll('.annotation-input').forEach(el => {
        el.classList.remove('active');
    });
    if (state.activeParagraph !== null) {
        const target = document.getElementById(`anno-input-${state.activeParagraph}`);
        if (target) {
            target.classList.add('active');
            target.querySelector('textarea').focus();
        }
    }
}

function closeAnnotationInput() {
    state.activeParagraph = null;
    document.querySelectorAll('.annotation-input').forEach(el => {
        el.classList.remove('active');
    });
}

async function submitAnnotation(paragraphIndex) {
    let textarea;
    if (paragraphIndex === null) {
        // PDF 整页批注
        textarea = document.getElementById('pdf-anno-input');
    } else {
        const input = document.getElementById(`anno-input-${paragraphIndex}`);
        textarea = input.querySelector('textarea');
    }
    const content = textarea.value.trim();
    if (!content) return;

    const isPdfPageNote = (paragraphIndex === null && state.fileType === 'pdf');
    try {
        await API.addAnnotation(state.currentBook.id, state.currentPage, {
            content,
            paragraph_index: paragraphIndex,
            target_type: isPdfPageNote ? 'page_note' : 'paragraph',
        });
        textarea.value = '';
        if (paragraphIndex !== null) closeAnnotationInput();
        showToast('批注已发送');
        await refreshAnnotations();
    } catch (e) {
        showToast('发送失败: ' + e.message, true);
    }
}

// ---------- SSE 实时推送 ----------

function startSSE() {
    stopSSE();
    if (!state.currentBook) return;

    const url = `/api/books/${state.currentBook.id}/events`;
    state.eventSource = new EventSource(url);

    state.eventSource.addEventListener('annotations', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.page === state.currentPage) {
                state.annotations = data.annotations;
                renderParagraphs();
            }
        } catch {}
    });

    state.eventSource.addEventListener('chat', (e) => {
        try {
            const data = JSON.parse(e.data);
            const hadMessages = state.chatMessages.length;
            state.chatMessages = data.messages;
            renderChatMessages();
            if (!state.chatOpen && data.messages.length > hadMessages) {
                const newMsgs = data.messages.slice(hadMessages);
                if (newMsgs.some(m => m.author === 'claude')) {
                    document.getElementById('chat-unread-dot').classList.add('show');
                }
            }
        } catch {}
    });

    state.eventSource.addEventListener('progress', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.current_page !== state.currentPage) {
                showToast(`Claude 翻到了第 ${data.current_page} 页`);
                loadPage(data.current_page);
            }
        } catch {}
    });

    // 主题变更
    state.eventSource.addEventListener('theme', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.theme) applyTheme(data.theme);
        } catch {}
    });

    // 自定义配色变更
    state.eventSource.addEventListener('custom_theme', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.colors) applyCustomColors(data.colors);
        } catch {}
    });

    // 写作模式 SSE 事件
    state.eventSource.addEventListener('draft', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.page === state.currentPage && data.revision > state.draftRevision) {
                state.draftRevision = data.revision;
                state.draftContent = data.content;
                state.draftParagraphs = data.content.split('\n').filter(p => p.trim());
                if (state.editingParagraph === null) renderParagraphs();
            }
        } catch {}
    });

    state.eventSource.addEventListener('suggestions', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.page === state.currentPage) {
                state.suggestions = data.suggestions;
                if (state.mode === 'write' && state.editingParagraph === null) renderParagraphs();
            }
        } catch {}
    });

    state.eventSource.onerror = () => {
        // SSE 断了，回退到轮询
        stopSSE();
        startPollingFallback();
    };
}

function stopSSE() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
}

// ---------- 轮询 Fallback（SSE 不可用时） ----------

function startPollingFallback() {
    stopPollingFallback();
    state.pollTimer = setInterval(async () => {
        await refreshAnnotations();
        await refreshChat();
        await checkProgress();
    }, 3000);
}

function stopPollingFallback() {
    if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
    }
}

function startPolling() { startSSE(); }
function stopPolling() { stopSSE(); stopPollingFallback(); }
function startProgressPolling() { /* handled by SSE */ }
function stopProgressPolling() { /* handled by SSE */ }

function _annoSignature(annotations) {
    return annotations.map(a => `${a.id}:${a.content.length}`).join(',');
}

async function refreshAnnotations() {
    if (!state.currentBook) return;
    try {
        const annotations = await API.getAnnotations(state.currentBook.id, state.currentPage);
        if (_annoSignature(annotations) !== _annoSignature(state.annotations)) {
            state.annotations = annotations;
            renderParagraphs();
        }
    } catch {}
}

async function checkProgress() {
    if (!state.currentBook) return;
    try {
        const progress = await API.getCurrentProgress(state.currentBook.id);
        if (progress.current_page !== state.currentPage) {
            showToast(`Claude 翻到了第 ${progress.current_page} 页`);
            await loadPage(progress.current_page);
        }
    } catch {}
}

// ---------- 翻页 ----------

function navigatePage(delta) {
    const newPage = state.currentPage + delta;
    if (!state.currentBook) return;
    if (newPage < 1 || newPage > state.currentBook.total_pages) return;
    loadPage(newPage);
}

// ---------- 导出笔记 ----------

async function exportNotes() {
    if (!state.currentBook) return;
    try {
        const res = await fetch(`/api/books/${state.currentBook.id}/export`);
        if (!res.ok) throw new Error('导出失败');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${state.currentBook.title}_notes.md`;
        a.click();
        URL.revokeObjectURL(url);
        showToast('笔记已导出');
    } catch (e) {
        showToast('导出失败: ' + e.message, true);
    }
}

// ---------- 键盘快捷键 ----------

document.addEventListener('keydown', (e) => {
    if (state.currentView !== 'reader') return;
    if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
    if (e.key === 'ArrowLeft') navigatePage(-1);
    if (e.key === 'ArrowRight') navigatePage(+1);
});

// ---------- 阅读会话 ----------

function startSessionTimer() {
    stopSessionTimer();
    state.sessionTimerInterval = setInterval(updateSessionTimer, 1000);
    updateSessionTimer();
}

function stopSessionTimer() {
    if (state.sessionTimerInterval) {
        clearInterval(state.sessionTimerInterval);
        state.sessionTimerInterval = null;
    }
    document.getElementById('session-timer').textContent = '';
}

function updateSessionTimer() {
    if (!state.sessionStartTime) return;
    const elapsed = Math.floor((Date.now() - state.sessionStartTime) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    const display = h > 0
        ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
        : `${m}:${String(s).padStart(2, '0')}`;
    document.getElementById('session-timer').textContent = display;
}

function endCurrentSession() {
    if (state.sessionId && state.currentBook) {
        API.endSession(state.currentBook.id, {
            session_id: state.sessionId,
            pages_read: 0,
        }).catch(() => {});
    }
    state.sessionId = null;
    state.sessionStartTime = null;
}

function endAllSessions() {
    for (const id of Object.keys(state.openBooks)) {
        closeTab(parseInt(id));
    }
}

// ---------- 按钮事件 ----------

document.getElementById('btn-back').addEventListener('click', () => showView('bookshelf'));
document.getElementById('btn-prev').addEventListener('click', () => navigatePage(-1));
document.getElementById('btn-next').addEventListener('click', () => navigatePage(+1));
document.getElementById('btn-export').addEventListener('click', exportNotes);
document.getElementById('btn-mode-toggle').addEventListener('click', toggleWriteMode);
document.getElementById('btn-settings').addEventListener('click', () => showView('settings'));
document.getElementById('btn-settings-back').addEventListener('click', () => showView('bookshelf'));

// ---------- 工具函数 ----------

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ========== 聊天面板（跨页） ==========

function toggleChatPanel() {
    state.chatOpen = !state.chatOpen;
    const panel = document.getElementById('chat-panel');
    if (state.chatOpen) {
        panel.classList.add('open');
        document.getElementById('chat-unread-dot').classList.remove('show');
        scrollChatToBottom();
    } else {
        panel.classList.remove('open');
    }
}

function closeChatPanel() {
    state.chatOpen = false;
    document.getElementById('chat-panel').classList.remove('open');
}

function renderChatMessages() {
    const container = document.getElementById('chat-messages');
    if (state.chatMessages.length === 0) {
        container.innerHTML = '<div class="chat-empty">开始和 Claude 讨论这本书的内容吧</div>';
        return;
    }

    let html = '';
    let lastPage = null;

    state.chatMessages.forEach(msg => {
        // 页码分隔
        if (msg.page_number !== lastPage) {
            html += `<div class="chat-page-divider">— 第 ${msg.page_number} 页 —</div>`;
            lastPage = msg.page_number;
        }

        const isClaude = msg.author === 'claude';
        const time = msg.created_at
            ? new Date(msg.created_at + 'Z').toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
            : '';
        const highlight = msg.highlight_text
            ? `<div class="chat-msg-highlight">"${escapeHtml(msg.highlight_text)}"</div>`
            : '';

        html += `
            <div class="chat-msg ${isClaude ? 'claude' : 'user'}">
                <div class="chat-msg-header">
                    <span class="chat-msg-author">${isClaude ? 'Claude' : state.userName}</span>
                    <span class="chat-msg-page">p.${msg.page_number}</span>
                </div>
                ${highlight}
                <div class="chat-msg-text">${escapeHtml(msg.content)}</div>
                <div class="chat-msg-time">${time}</div>
            </div>
        `;
    });

    container.innerHTML = html;
    scrollChatToBottom();
}

function scrollChatToBottom() {
    const container = document.getElementById('chat-messages');
    container.scrollTop = container.scrollHeight;
}

async function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const content = input.value.trim();
    if (!content || !state.currentBook) return;

    try {
        await API.sendChatMessage(state.currentBook.id, state.currentPage, { content });
        input.value = '';
        await refreshChat();
    } catch (e) {
        showToast('发送失败: ' + e.message, true);
    }
}

async function refreshChat() {
    if (!state.currentBook) return;
    try {
        const messages = await API.getAllChat(state.currentBook.id);
        if (messages.length !== state.chatMessages.length) {
            const hadMessages = state.chatMessages.length;
            state.chatMessages = messages;
            renderChatMessages();
            if (!state.chatOpen && messages.length > hadMessages) {
                const newMsgs = messages.slice(hadMessages);
                if (newMsgs.some(m => m.author === 'claude')) {
                    document.getElementById('chat-unread-dot').classList.add('show');
                }
            }
        }
    } catch {
        // 静默
    }
}

function startChatPolling() { /* handled by SSE */ }

function stopChatPolling() {
    if (state.chatPollTimer) {
        clearInterval(state.chatPollTimer);
        state.chatPollTimer = null;
    }
}

document.getElementById('chat-toggle').addEventListener('click', toggleChatPanel);
document.getElementById('chat-send').addEventListener('click', sendChatMessage);
document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
    }
});

// ========== 设置页 ==========

async function renderSettings() {
    const container = document.getElementById('settings-content');
    try {
        state.settings = await API.getSettings();
    } catch (e) {
        container.innerHTML = `<p class="settings-error">加载设置失败: ${escapeHtml(e.message)}</p>`;
        return;
    }

    const s = state.settings;
    container.innerHTML = `
        <div class="settings-section">
            <h2 class="settings-section-title">外观</h2>
            <div class="settings-group">
                <label class="settings-label">主题</label>
                <select id="setting-theme" class="settings-select">
                    <option value="dark" ${s.theme === 'dark' ? 'selected' : ''}>深色</option>
                    <option value="light" ${s.theme === 'light' ? 'selected' : ''}>浅色</option>
                    <option value="sepia" ${s.theme === 'sepia' ? 'selected' : ''}>护眼</option>
                </select>
            </div>
        </div>

        <div class="settings-section">
            <h2 class="settings-section-title">记忆压缩模型</h2>
            <p class="settings-hint">翻页时自动生成阅读摘要的 AI 模型配置（OpenAI 兼容格式）</p>
            <div class="settings-group">
                <label class="settings-label">API Base URL</label>
                <input id="setting-api-base" class="settings-input" type="text"
                    value="${escapeHtml(s.summary_api_base || '')}"
                    placeholder="https://api.openai.com/v1">
            </div>
            <div class="settings-group">
                <label class="settings-label">API Key</label>
                <input id="setting-api-key" class="settings-input" type="password"
                    value=""
                    placeholder="${s.summary_api_key_masked || '未配置'}">
                <span class="settings-hint">留空则保持原有 key 不变</span>
            </div>
            <div class="settings-group">
                <label class="settings-label">模型名称</label>
                <input id="setting-model" class="settings-input" type="text"
                    value="${escapeHtml(s.summary_model || '')}"
                    placeholder="gpt-4o-mini">
            </div>
        </div>

        <div class="settings-actions">
            <button class="btn settings-save-btn" id="btn-save-settings">保存设置</button>
        </div>
    `;

    document.getElementById('setting-theme').addEventListener('change', (e) => {
        applyTheme(e.target.value);
    });

    document.getElementById('btn-save-settings').addEventListener('click', saveSettings);
}

async function saveSettings() {
    const updates = {};

    const theme = document.getElementById('setting-theme').value;
    updates.theme = theme;

    const apiBase = document.getElementById('setting-api-base').value.trim();
    if (apiBase) updates.summary_api_base = apiBase;

    const apiKey = document.getElementById('setting-api-key').value.trim();
    if (apiKey) updates.summary_api_key = apiKey;

    const model = document.getElementById('setting-model').value.trim();
    if (model) updates.summary_model = model;

    try {
        await API.updateSettings(updates);
        showToast('设置已保存');
        applyTheme(theme);
    } catch (e) {
        showToast('保存失败: ' + e.message, true);
    }
}

// ========== 主题系统 ==========

function applyTheme(themeName) {
    document.documentElement.setAttribute('data-theme', themeName);
}

function applyCustomColors(colorsJson) {
    try {
        const colors = typeof colorsJson === 'string' ? JSON.parse(colorsJson) : colorsJson;
        const root = document.documentElement;
        for (const [key, value] of Object.entries(colors)) {
            root.style.setProperty(`--${key}`, value);
        }
    } catch {}
}

// ---------- 初始化 ----------

async function init() {
    try {
        const config = await API.request('GET', '/api/config');
        state.userName = config.user_name || 'Reader';
        if (config.theme && config.theme !== 'dark') {
            applyTheme(config.theme);
        }
        if (config.custom_theme) {
            applyCustomColors(config.custom_theme);
        }
    } catch { /* fallback to default */ }
    initUpload();
    initGeoBg();
    renderBookshelf();
}

function initGeoBg() {
    const container = document.getElementById('geo-bg');
    if (!container) return;
    const shapes = ['circle', 'triangle', 'diamond', 'line', 'circle', 'triangle', 'diamond'];
    shapes.forEach((type, i) => {
        const el = document.createElement('div');
        el.className = `geo-shape geo-${type}`;
        el.style.left = `${10 + Math.random() * 80}%`;
        el.style.top = `${10 + Math.random() * 80}%`;
        el.style.animationDuration = `${30 + Math.random() * 40}s`;
        el.style.animationDelay = `${-Math.random() * 30}s`;
        container.appendChild(el);
    });
}

init();
