document.addEventListener('DOMContentLoaded', function() {
    // Auto-hide flash messages
    const flashes = document.querySelectorAll('.flash');
    flashes.forEach(function(flash) {
        setTimeout(function() {
            flash.style.opacity = '0';
            flash.style.transition = 'opacity 0.5s';
            setTimeout(function() { flash.remove(); }, 500);
        }, 5000);
    });

    // Confirm before download
    const form = document.getElementById('descargar-form');
    if (form) {
        form.addEventListener('submit', function(e) {
            const btn = document.getElementById('btn-descargar');
            if (btn) {
                btn.disabled = true;
                btn.textContent = 'Descargando... Por favor espere';
            }
        });
    }

    // Header date defaults
    const fechaInicio = document.getElementById('fecha_inicio');
    const fechaFin = document.getElementById('fecha_fin');
    if (fechaInicio && fechaFin && !fechaInicio.value) {
        const hoy = new Date();
        const primerDia = new Date(hoy.getFullYear(), hoy.getMonth(), 1);
        fechaInicio.value = primerDia.toISOString().split('T')[0];
        fechaFin.value = hoy.toISOString().split('T')[0];
    }

    // Mobile menu toggle
    const navToggle = document.querySelector('.nav-toggle');
    const navLinks = document.querySelector('.nav-links');
    if (navToggle && navLinks) {
        navToggle.addEventListener('click', function() {
            navLinks.classList.toggle('open');
        });
        navLinks.querySelectorAll('a').forEach(function(link) {
            link.addEventListener('click', function() {
                navLinks.classList.remove('open');
            });
        });
    }
});

function toggleDropdown(event) {
    event.preventDefault();
    const dd = event.currentTarget.closest('.nav-dropdown');
    if (dd) dd.classList.toggle('open');
}

document.addEventListener('click', function(event) {
    const open = document.querySelectorAll('.nav-dropdown.open');
    open.forEach(function(dd) {
        if (!dd.contains(event.target)) dd.classList.remove('open');
    });
});
