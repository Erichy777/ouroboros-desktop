import { escapeHtml, renderMarkdown } from './utils.js';

export function initChat({ ws, state, updateUnreadBadge }) {
    const container = document.getElementById('content');

    const page = document.createElement('div');
    page.id = 'page-chat';
    page.className = 'page active';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <h2>Chat</h2>
            <div class="spacer"></div>
            <span id="chat-status" class="status-badge offline">Connecting...</span>
        </div>
        <div id="chat-messages"></div>
        <div id="chat-input-area">
            <textarea id="chat-input" placeholder="Message Ouroboros..." rows="1"></textarea>
            <button class="icon-btn" id="chat-send">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
        </div>
    `;
    container.appendChild(page);

    const messagesDiv = document.getElementById('chat-messages');
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('chat-send');

    const _chatHistory = [];

    function formatMsgTime(isoStr) {
        if (!isoStr) return null;
        try {
            const d = new Date(isoStr);
            if (isNaN(d)) return null;
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
            const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            const todayStr = now.toDateString();
            const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
            let short;
            if (d.toDateString() === todayStr) {
                short = hhmm;
            } else if (d.toDateString() === yesterday.toDateString()) {
                short = `Yesterday, ${hhmm}`;
            } else {
                short = `${months[d.getMonth()]} ${d.getDate()}, ${hhmm}`;
            }
            const full = `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} at ${hhmm}`;
            return { short, full };
        } catch { return null; }
    }

    function addMessage(text, role, markdown = false, timestamp = null, isProgress = false) {
        const ts = timestamp || new Date().toISOString();
        if (!isProgress) _chatHistory.push({ text, role, ts });
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}` + (isProgress ? ' progress' : '');
        const sender = role === 'user' ? 'You' : (isProgress ? '\uD83D\uDCAC Ouroboros' : 'Ouroboros');
        const rendered = role === 'assistant' ? renderMarkdown(text) : escapeHtml(text);
        const timeFmt = formatMsgTime(ts);
        const timeHtml = timeFmt
            ? `<div class="msg-time" title="${timeFmt.full}">${timeFmt.short}</div>`
            : '';
        bubble.innerHTML = `
            <div class="sender">${sender}</div>
            <div class="message">${rendered}</div>
            ${timeHtml}
        `;
        const typing = document.getElementById('typing-indicator');
        if (typing && typing.parentNode === messagesDiv) {
            messagesDiv.insertBefore(bubble, typing);
        } else {
            messagesDiv.appendChild(bubble);
        }
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
        try { sessionStorage.setItem('ouro_chat', JSON.stringify(_chatHistory.slice(-200))); } catch {}
    }

    // Restore chat history from server (persists across app restarts)
    (async () => {
        try {
            const resp = await fetch('/api/chat/history?limit=1000');
            if (resp.ok) {
                const data = await resp.json();
                if (data.messages && data.messages.length > 0) {
                    for (const msg of data.messages) addMessage(msg.text, msg.role, false, msg.ts || null, !!msg.is_progress);
                    return;
                }
            }
        } catch (err) { console.error('Failed to load chat history:', err); }
        // Fallback: sessionStorage (survives page reload but not app restart)
        try {
            const saved = JSON.parse(sessionStorage.getItem('ouro_chat') || '[]');
            for (const msg of saved) addMessage(msg.text, msg.role, false, msg.ts || null);
        } catch {}
    })();

    function sendMessage() {
        const text = input.value.trim();
        if (!text) return;
        input.value = '';
        input.style.height = 'auto';
        addMessage(text, 'user');
        ws.send({ type: 'chat', content: text });
    }

    sendBtn.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });

    // Typing indicator element (persistent, shown/hidden as needed)
    const typingEl = document.createElement('div');
    typingEl.id = 'typing-indicator';
    typingEl.className = 'chat-bubble assistant typing-bubble';
    typingEl.style.display = 'none';
    typingEl.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;
    messagesDiv.appendChild(typingEl);

    function showTyping() {
        typingEl.style.display = '';
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
        const badge = document.getElementById('chat-status');
        if (badge) {
            badge.className = 'status-badge thinking';
            badge.textContent = 'Thinking...';
        }
    }
    function hideTyping() {
        typingEl.style.display = 'none';
        const badge = document.getElementById('chat-status');
        if (badge && badge.textContent === 'Thinking...') {
            badge.className = 'status-badge online';
            badge.textContent = 'Online';
        }
    }

    ws.on('typing', () => { showTyping(); });

    ws.on('chat', (msg) => {
        if (msg.role === 'assistant') {
            hideTyping();
            addMessage(msg.content, 'assistant', msg.markdown, msg.ts || null, !!msg.is_progress);
            if (state.activePage !== 'chat') {
                state.unreadCount++;
                updateUnreadBadge();
            }
        }
    });

    ws.on('photo', (msg) => {
        hideTyping();
        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble assistant';
        const timeFmt = formatMsgTime(msg.ts || new Date().toISOString());
        const timeHtml = timeFmt
            ? `<div class="msg-time" title="${timeFmt.full}">${timeFmt.short}</div>`
            : '';
        const captionHtml = msg.caption ? `<div class="message">${escapeHtml(msg.caption)}</div>` : '';
        bubble.innerHTML = `
            <div class="sender">Ouroboros</div>
            ${captionHtml}
            <div class="message"><img src="data:image/png;base64,${msg.image_base64}" style="max-width:100%;border-radius:8px;cursor:pointer" onclick="window.open(this.src,'_blank')" /></div>
            ${timeHtml}
        `;
        const typing = document.getElementById('typing-indicator');
        const messagesDiv = document.getElementById('chat-messages');
        if (typing && typing.parentNode === messagesDiv) {
            messagesDiv.insertBefore(bubble, typing);
        } else {
            messagesDiv.appendChild(bubble);
        }
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
        if (state.activePage !== 'chat') {
            state.unreadCount++;
            updateUnreadBadge();
        }
    });

    ws.on('open', () => {
        document.getElementById('chat-status').className = 'status-badge online';
        document.getElementById('chat-status').textContent = 'Online';
    });
    ws.on('close', () => {
        hideTyping();
        document.getElementById('chat-status').className = 'status-badge offline';
        document.getElementById('chat-status').textContent = 'Reconnecting...';
    });

}
