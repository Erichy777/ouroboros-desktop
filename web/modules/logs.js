import { escapeHtml } from './utils.js';

export function initLogs({ ws, state }) {
    const categories = {
        tools: { label: 'Tools', color: 'var(--blue)' },
        llm: { label: 'LLM', color: 'var(--accent)' },
        errors: { label: 'Errors', color: 'var(--red)' },
        tasks: { label: 'Tasks', color: 'var(--amber)' },
        system: { label: 'System', color: 'var(--text-muted)' },
        consciousness: { label: 'Consciousness', color: 'var(--accent)' },
    };

    const page = document.createElement('div');
    page.id = 'page-logs';
    page.className = 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            <h2>Logs</h2>
            <div class="spacer"></div>
            <button class="btn btn-default" id="btn-clear-logs">Clear</button>
        </div>
        <div class="logs-filters" id="log-filters"></div>
        <div id="log-entries"></div>
    `;
    document.getElementById('content').appendChild(page);

    const filtersDiv = document.getElementById('log-filters');
    Object.entries(categories).forEach(([key, cat]) => {
        const chip = document.createElement('button');
        chip.className = `filter-chip ${state.activeFilters[key] ? 'active' : ''}`;
        chip.textContent = cat.label;
        chip.addEventListener('click', () => {
            state.activeFilters[key] = !state.activeFilters[key];
            chip.classList.toggle('active');
            logEntries.querySelectorAll('.log-entry').forEach(el => {
                const entryCat = el.dataset.category;
                if (entryCat) {
                    el.style.display = state.activeFilters[entryCat] ? '' : 'none';
                }
            });
        });
        filtersDiv.appendChild(chip);
    });

    const logEntries = document.getElementById('log-entries');
    const MAX_LOGS = 500;

    function categorizeEvent(evt) {
        const t = evt.type || evt.event || '';
        if (t.includes('error') || t.includes('crash') || t.includes('fail')) return 'errors';
        if (t.includes('llm') || t.includes('model')) return 'llm';
        if (t.includes('tool') || evt.tool) return 'tools';
        if (t.includes('task') || t.includes('evolution') || t.includes('review')) return 'tasks';
        if (t.includes('consciousness') || t.includes('bg_')) return 'consciousness';
        return 'system';
    }

    const LOG_PREVIEW_LEN = 200;

    function buildLogMessage(evt) {
        const t = evt.type || evt.event || '';
        let parts = [];
        if (evt.task_id) parts.push(`[${evt.task_id}]`);

        if (t === 'llm_round' || t === 'llm_usage') {
            if (evt.model) parts.push(evt.model);
            if (evt.round) parts.push(`r${evt.round}`);
            if (evt.prompt_tokens) parts.push(`${evt.prompt_tokens}\u2192${evt.completion_tokens || 0}tok`);
            if (evt.cost_usd) parts.push(`$${Number(evt.cost_usd).toFixed(4)}`);
            else if (evt.cost) parts.push(`$${Number(evt.cost).toFixed(4)}`);
        } else if (t === 'task_eval' || t === 'task_done') {
            if (evt.task_type) parts.push(evt.task_type);
            if (evt.duration_sec) parts.push(`${evt.duration_sec.toFixed(1)}s`);
            if (evt.tool_calls != null) parts.push(`${evt.tool_calls} tools`);
            if (evt.cost_usd) parts.push(`$${Number(evt.cost_usd).toFixed(4)}`);
            if (evt.total_rounds) parts.push(`${evt.total_rounds} rounds`);
            if (evt.response_len) parts.push(`${evt.response_len} chars`);
        } else if (t === 'task_received') {
            const task = evt.task || {};
            if (task.type) parts.push(task.type);
            if (task.text) parts.push(task.text.slice(0, 100));
        } else if (t === 'tool_call' || evt.tool) {
            if (evt.tool) parts.push(evt.tool);
            if (evt.args) {
                const a = JSON.stringify(evt.args);
                parts.push(a.length > 300 ? a.slice(0, 300) + '...' : a);
            }
            if (evt.result_preview) parts.push('\u2192 ' + evt.result_preview.slice(0, 500));
        } else if (t.includes('error') || t.includes('crash') || t.includes('fail')) {
            if (evt.error) parts.push(evt.error);
            if (evt.tool) parts.push(`tool=${evt.tool}`);
        } else {
            if (evt.model) parts.push(evt.model);
            if (evt.cost) parts.push(`$${Number(evt.cost).toFixed(4)}`);
            if (evt.cost_usd) parts.push(`$${Number(evt.cost_usd).toFixed(4)}`);
            if (evt.error) parts.push(evt.error);
        }
        if (evt.text) parts.push(evt.text.slice(0, 2000));
        return parts.join(' ');
    }

    function addLogEntry(evt) {
        const cat = categorizeEvent(evt);

        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.dataset.category = cat;
        const ts = (evt.ts || '').slice(11, 19);
        const type = evt.type || evt.event || 'unknown';
        let msg = buildLogMessage(evt);

        const isLong = msg.length > LOG_PREVIEW_LEN;
        const preview = isLong ? msg.slice(0, LOG_PREVIEW_LEN) + '...' : msg;

        entry.innerHTML = `
            <span class="log-ts">${ts}</span>
            <span class="log-type ${cat}">${type}</span>
            <span class="log-msg">${escapeHtml(preview)}</span>
        `;
        if (isLong) {
            entry.style.cursor = 'pointer';
            entry.title = 'Click to expand';
            let expanded = false;
            entry.addEventListener('click', () => {
                const msgEl = entry.querySelector('.log-msg');
                expanded = !expanded;
                msgEl.textContent = expanded ? msg : preview;
            });
        }
        if (!state.activeFilters[cat]) entry.style.display = 'none';
        logEntries.appendChild(entry);

        while (logEntries.children.length > MAX_LOGS) {
            logEntries.removeChild(logEntries.firstChild);
        }
        if (state.activeFilters[cat]) logEntries.scrollTop = logEntries.scrollHeight;
    }

    ws.on('log', (msg) => {
        if (msg.data) addLogEntry(msg.data);
    });

    document.getElementById('btn-clear-logs').addEventListener('click', () => {
        logEntries.innerHTML = '';
    });
}
