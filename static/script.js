let currentCount = 0; let userPoints = 0; let logoClicks = 0; let currentSlot = 0;
const bgMusic = new Audio('/static/relaxing.mp3');
const dropSound = new Audio('/static/bottle_drop.mp3');
bgMusic.loop = true; bgMusic.volume = 0.3;

function setPoints(pts) { userPoints = pts; }
document.addEventListener('click', () => { if (bgMusic.paused) bgMusic.play(); }, { once: true });

function updateStatus() {
    fetch('/api/status').then(r => r.json()).then(data => {
        let anyBusy = false; let timerHtml = "";
        for (let i = 0; i <= 3; i++) {
            const btn = document.getElementById(`btn-slot-${i}`);
            const secs = data.slots[i];
            if (secs > 0) {
                anyBusy = true; btn.innerText = "BUSY"; btn.classList.add('busy'); btn.onclick = null;
                let m = Math.floor(secs / 60); let s = secs % 60;
                timerHtml += `Slot ${i+1}: ${m}:${s < 10 ? '0'+s : s} | `;
            } else {
                btn.innerText = "Redeem"; btn.classList.remove('busy'); btn.onclick = () => openRedeemModal(i);
            }
        }
        const banner = document.getElementById('timer-banner');
        if (anyBusy) { banner.style.display = 'block'; banner.innerText = timerHtml.slice(0, -3); }
        else { banner.style.display = 'none'; }
    });
}
setInterval(updateStatus, 1000);

function startSession() {
    currentCount = 0; document.getElementById('insert-modal').style.display = 'flex';
    fetch('/api/start_session').then(() => {
        window.loop = setInterval(() => {
            fetch('/api/status').then(r => r.json()).then(d => {
                if (d.session > currentCount) { dropSound.currentTime = 0; dropSound.play(); currentCount = d.session; }
                document.getElementById('live-count').innerText = d.session;
            });
        }, 500);
    });
}
function stopSession() { clearInterval(window.loop); fetch('/api/stop_session').then(() => location.reload()); }

function handleLogoClick() {
    logoClicks++; if (logoClicks >= 5) { document.getElementById('admin-auth').style.display = 'flex'; logoClicks = 0; }
    setTimeout(() => logoClicks = 0, 3000);
}
function loginAdmin() {
    if (document.getElementById('admin-pass-input').value === "eco123") {
        document.getElementById('admin-auth').style.display = 'none';
        document.getElementById('admin-panel').style.display = 'flex';
        refreshAdmin();
    } else { alert("Access Denied"); }
    document.getElementById('admin-pass-input').value = "";
}
function refreshAdmin() {
    fetch('/api/admin_stats').then(r => r.json()).then(data => {
        document.getElementById('total-bottles').innerText = data.total_bottles;
        let html = '';
        data.users.forEach(u => {
            html += `<div class="admin-row">
                <div style="text-align:left;"><div style="font-size:0.7rem;color:#888;">${u.user_id}</div><div style="font-weight:800;color:var(--orange);">${u.points} Pts</div></div>
                <div style="display:flex;gap:5px;">
                    <button style="width:30px;height:30px;background:#444;color:white;border:none;border-radius:5px;" onclick="updateAdminPts('${u.user_id}','sub')">-</button>
                    <button style="width:30px;height:30px;background:var(--green);color:white;border:none;border-radius:5px;" onclick="updateAdminPts('${u.user_id}','add')">+</button>
                </div>
            </div>`;
        });
        document.getElementById('admin-list').innerHTML = html;
    });
}
function updateAdminPts(uid, action) { fetch(`/api/admin_update_points?uid=${uid}&action=${action}`).then(() => refreshAdmin()); }
function emergencyReset() { if(confirm("STOP ALL SLOTS?")) fetch('/api/admin_reset').then(() => location.reload()); }
function openRedeemModal(id) { currentSlot = id; document.getElementById('redeem-modal').style.display = 'flex'; }
function adjustPoints(val) { 
	let i = document.getElementById('pts-to-redeem'); 
	let c = parseInt(i.value); 
	if (c + val >= 1) i.value = c + val; }
function confirmRedeem() {
    const req = parseInt(document.getElementById('pts-to-redeem').value);
    if (req > userPoints) { 
        alert("Insufficient Points"); 
        return; 
    }
    location.href = `/redeem/${currentSlot}/${req}`;
}
function closeRedeemModal() { 
    document.getElementById('redeem-modal').style.display = 'none'; 
}
