/**
 * WebSocket Manager module.
 *
 * Connection is deferred: call ws.connect() AFTER all modules
 * have registered their event listeners to avoid race conditions.
 */

export class WS {
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.listeners = {};
        this.reconnectDelay = 1000;
        this.maxDelay = 10000;
        this._wasConnected = false;
        this._lastSha = null;
        // Do NOT connect here — wait for all modules to register listeners first
    }

    connect() {
        this.ws = new WebSocket(this.url);
        this.ws.onopen = () => {
            if (this._wasConnected) {
                fetch('/api/state').then(r => r.json()).then(d => {
                    if (this._lastSha && d.sha && d.sha !== this._lastSha) {
                        location.reload();
                    } else {
                        this._lastSha = d.sha || this._lastSha;
                        this.reconnectDelay = 1000;
                        this.emit('open');
                        document.getElementById('reconnect-overlay')?.classList.remove('visible');
                    }
                }).catch(() => location.reload());
                return;
            }
            this._wasConnected = true;
            fetch('/api/state').then(r => r.json()).then(d => {
                this._lastSha = d.sha || null;
            }).catch(() => {});
            this.reconnectDelay = 1000;
            this.emit('open');
            document.getElementById('reconnect-overlay')?.classList.remove('visible');
        };
        this.ws.onclose = () => {
            this.emit('close');
            document.getElementById('reconnect-overlay')?.classList.add('visible');
            setTimeout(() => this.connect(), this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxDelay);
        };
        this.ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                this.emit('message', msg);
                if (msg.type) this.emit(msg.type, msg);
            } catch {}
        };
    }

    send(msg) {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(msg));
        }
    }

    on(event, fn) {
        (this.listeners[event] ||= []).push(fn);
    }

    emit(event, data) {
        (this.listeners[event] || []).forEach(fn => fn(data));
    }
}

export function createWS() {
    return new WS(`ws://${location.host}/ws`);
}
