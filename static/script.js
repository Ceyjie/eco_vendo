let currentSlot = 0;
let counterInterval;
let logoClicks = 0;
let logoTimer;
let selectedSlot = null;


// --- HIDDEN ADMIN ACCESS ---
function handleLogoClick() {
    logoClicks++;
    clearTimeout(logoTimer);
    if (logoClicks === 5) {
        document.getElementById('admin-login-modal').style.display = 'flex';
        logoClicks = 0;
    }
    logoTimer = setTimeout(() => { logoClicks = 0; }, 3000);
}

function verifyAdmin() {
    const pass = document.getElementById('admin-pass-input').value;
    if (pass === "eco123") {
        document.getElementById('admin-login-modal').style.display = 'none';
        document.getElementById('admin-panel-modal').style.display = 'flex';
        fetchAdminData(pass);
    } else { alert("Incorrect Password"); }
}

function fetchAdminData(pwd) {
    fetch(`/api/admin_stats?pwd=${pwd}`)
        .then(r => r.json())
        .then(data => {
            let html = '<table class="admin-table"><tr><th>User ID</th><th>Points</th></tr>';
            data.users.forEach(u => {
                html += `<tr><td>${u.user_id}</td><td>${u.points}</td></tr>`;
            });
            document.getElementById('admin-user-list').innerHTML = html + '</table>';
        });
}

function resetAll() {
    const pwd = document.getElementById('admin-pass-input').value;
    if(confirm("Stop all charging sessions immediately?")) {
        fetch(`/api/reset_all_timers?pwd=${pwd}`)
            .then(r => r.json())
            .then(d => { alert(d.status); location.reload(); });
    }
}

// --- CORE FUNCTIONS ---
function startSession() {
    document.getElementById('modal').style.display = 'flex';
    fetch('/api/start_session').then(() => {
        counterInterval = setInterval(() => {
            fetch('/api/get_count').then(r => r.json()).then(d => {
                document.getElementById('count').innerText = d.count;
            });
        }, 800);
    });
}

function stopSession() {
    clearInterval(counterInterval);
    fetch('/api/stop_session').then(() => { location.reload(); });
}

function redeem(slotId) {
    if(confirm("Use 1 point to activate this slot?")) {
        window.location.href = `/redeem/${slotId}/1`;
    }
}

function openRedeemModal(slotId, name) {
    selectedSlot = slotId;
    document.getElementById('modal-slot-name').innerText = "Use Points for " + name;
    document.getElementById('redeem-confirm-modal').style.display = 'flex';
    
    // Auto-update the "Minutes" preview when typing
    document.getElementById('pts-to-use').addEventListener('input', function() {
        let pts = parseInt(this.value) || 0;
        document.getElementById('time-preview').innerText = "= " + (pts * 5) + " Minutes";
    });
}


// Sends the final request to Python
function confirmRedeem() {
    let pts = document.getElementById('pts-to-use').value;
    if (pts < 1) {
        alert("Please enter at least 1 point.");
        return;
    }
    // This sends the Slot ID and the Amount of Points to your Python route
    window.location.href = `/redeem/${selectedSlot}/${pts}`;
}


function closeModal(id) { document.getElementById(id).style.display = 'none'; }

// --- SYNC ENGINE (Updates all connected phones) ---
setInterval(() => {
    fetch('/api/active_timers').then(r => r.json()).then(timers => {
        const list = document.getElementById('timer-list');
        list.innerHTML = "";
        for(let i=0; i<4; i++) {
            let s = document.getElementById('slot-'+i);
            if (s) {
                let isBusy = timers[i] !== undefined;
                s.querySelector('button').disabled = isBusy;
                s.querySelector('.busy-label').style.display = isBusy ? 'block' : 'none';
            }
        }
        for (let id in timers) {
            let t = timers[id];
            let m = Math.floor(t.remaining / 60), s = t.remaining % 60;
            list.innerHTML += `<div class="card" style="border:2px solid #2196F3;">⚡ ${t.name} ACTIVE: ${m}:${s<10?'0':''}${s}</div>`;
        }
    });
}, 1000);
