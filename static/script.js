let currentCount = 0;
let logoClicks = 0;
let targetSlot = 0;
const dropSound = new Audio('/static/bottle_drop.mp3');

function updateStatus() {
    fetch('/api/status').then(r => r.json()).then(data => {
        document.querySelector('.points-display').innerText = `${data.points} Points`;

        // Sound Fix: Only play for the active user
        if (data.is_my_session && data.session > currentCount) {
            dropSound.currentTime = 0;
            dropSound.play();
            currentCount = data.session;
        }

        // Live Count
        if (document.getElementById('live-count')) {
            document.getElementById('live-count').innerText = data.session;
        }

        // Timers
        let timerHtml = "";
        data.slots.forEach((secs, i) => {
            const btn = document.getElementById(`btn-slot-${i}`);
            if (secs > 0) {
                const mins = Math.floor(secs / 60);
                const s = secs % 60;
                timerHtml += `S${i + 1}: ${mins}m ${s}s | `;
                if (btn) btn.innerHTML = "ADD TIME";
            } else {
                if (btn) btn.innerHTML = "Redeem";
            }
        });

        const banner = document.getElementById('timer-banner');
        if (banner) {
            // Remove trailing " | "
            banner.innerText = timerHtml.replace(/ \| $/, '');
            banner.style.display = timerHtml ? 'block' : 'none';
        }
    }).catch(err => {
        console.warn("Status fetch failed:", err);
    });
}
setInterval(updateStatus, 1000);

function startSession() {
    fetch('/api/start_session').then(r => {
        if (r.status === 403) {
            alert("Machine Busy! Please wait.");
        } else {
            currentCount = 0; // Reset local counter for new session
            document.getElementById('insert-modal').style.display = 'flex';
        }
    }).catch(() => alert("Connection error. Try again."));
}

function stopSession() {
    fetch('/api/stop_session').then(() => {
        document.getElementById('insert-modal').style.display = 'none';
        location.reload();
    }).catch(() => {
        document.getElementById('insert-modal').style.display = 'none';
        location.reload();
    });
}

// Modal Handlers
function openRedeemModal(slot) {
    targetSlot = slot;
    document.getElementById('pts-to-redeem').value = 1;
    document.getElementById('redeem-modal').style.display = 'flex';
}

function closeRedeemModal() {
    document.getElementById('redeem-modal').style.display = 'none';
}

function adjustPoints(val) {
    const input = document.getElementById('pts-to-redeem');
    const currentPtsText = document.querySelector('.points-display').innerText;
    const currentPts = parseInt(currentPtsText);
    const nextVal = parseInt(input.value) + val;
    if (nextVal >= 1 && nextVal <= currentPts) {
        input.value = nextVal;
    }
}

function confirmRedeem() {
    const pts = document.getElementById('pts-to-redeem').value;
    window.location.href = `/redeem/${targetSlot}/${pts}`;
}

// Admin
function handleLogoClick() {
    logoClicks++;
    if (logoClicks >= 5) {
        document.getElementById('admin-auth').style.display = 'flex';
        logoClicks = 0;
    }
}

function loginAdmin() {
    const pass = document.getElementById('admin-pass-input').value;
    if (pass === "1234") {
        document.getElementById('admin-auth').style.display = 'none';
        document.getElementById('admin-panel').style.display = 'flex';
        fetchAdmin();
    } else {
        alert("Wrong password!");
    }
}

function fetchAdmin() {
    const pass = document.getElementById('admin-pass-input').value;
    fetch(`/api/admin_stats?pass=${pass}`)
        .then(r => r.json())
        .then(data => {
            if (data.error) return alert("Access Denied");

            document.getElementById('total-bottles').innerText = data.total_bottles;
            let html = "";

            data.users.forEach(u => {
                html += `
                <div class="admin-row" style="display:flex; justify-content:space-between; padding:5px; border-bottom:1px solid #444;">
                    <span style="font-family:monospace; color:#aaa;">${u.user_id}</span>
                    <b style="color:var(--green);">${u.points} Pts</b>
                </div>`;
            });

            document.getElementById('admin-list').innerHTML = html || "No users yet";
        })
        .catch(() => alert("Failed to fetch admin data."));
}

function emergencyReset() {
    if (confirm("Refresh all pins and stop all timers?")) {
        fetch('/api/emergency_reset')
            .then(r => r.json())
            .then(data => {
                alert("System Refreshed!");
                location.reload();
            })
            .catch(() => {
                alert("Reset sent (connection dropped, reloading...)");
                location.reload();
            });
    }
}
