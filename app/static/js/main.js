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

    // Set default dates
    const fechaInicio = document.getElementById('fecha_inicio');
    const fechaFin = document.getElementById('fecha_fin');
    if (fechaInicio && fechaFin && !fechaInicio.value) {
        const hoy = new Date();
        const primerDia = new Date(hoy.getFullYear(), hoy.getMonth(), 1);
        fechaInicio.value = primerDia.toISOString().split('T')[0];
        fechaFin.value = hoy.toISOString().split('T')[0];
    }
});
