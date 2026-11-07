/* ==========================================================================
   HoneyChain dashboard client
   --------------------------------------------------------------------------
   Transport strategy
     All data endpoints require a bearer token (Phase 3). Every request sends
     the token stored by the sign-in page (/app/, sessionStorage key
     "honeychain.token"). Because EventSource cannot send an Authorization
     header, live updates use ONE authenticated polling timer (every 3 s);
     a 401 stops polling and asks the user to sign in.

   `pollTimer` is a module-scoped handle that is always cleared before a new
   one is created, so duplicate timers cannot accumulate.

   Rendering safety
     Every value that originates from telemetry, Firebase or the ledger is
     written with `textContent`, never `innerHTML`. `escapeHtml()` is used for
     the few places a string is composed into markup. Untrusted input therefore
     cannot introduce nodes into the DOM.
   ========================================================================== */

(function () {
    'use strict';

    // ---------------------------------------------------------------- state
    var telemetryRecords = [];
    var activeMetric = 'temperature';
    var realtimeChart = null;
    var riskChart = null;

    var pollTimer = null;          // exactly one polling interval, ever

    var POLL_INTERVAL_MS = 3000;
    // Bearer token issued by POST /api/auth/login. Shared with the React
    // console (/app/), which stores it under the same sessionStorage key, so
    // signing in there authorises this dashboard in the same browser tab.
    var TOKEN_KEY = 'honeychain.token';
    var CHART_POINTS = 20;
    var EM_DASH = '--';

    var METRIC_LABELS = {
        temperature: 'Temperature (°C)',
        humidity: 'Humidity (%)',
        weight: 'Weight (kg)',
        audio_frequency: 'Acoustic frequency (Hz)'
    };

    // -------------------------------------------------------------- helpers
    function escapeHtml(value) {
        if (value === null || value === undefined || value === '') { return EM_DASH; }
        return String(value).replace(/[&<>"'`=\/]/g, function (character) {
            return {
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
                "'": '&#39;', '`': '&#96;', '=': '&#61;', '/': '&#47;'
            }[character];
        });
    }

    /* Format a number, returning '--' for null/undefined/non-finite.
       Note 0 is a legitimate reading and must NOT collapse to '--'. */
    function formatNumber(value, digits, suffix) {
        if (value === null || value === undefined) { return EM_DASH; }
        var number = Number(value);
        if (!isFinite(number)) { return EM_DASH; }
        return number.toFixed(digits) + (suffix || '');
    }

    function formatText(value) {
        if (value === null || value === undefined || value === '') { return EM_DASH; }
        return String(value);
    }

    function setText(elementId, value) {
        var element = document.getElementById(elementId);
        if (element) { element.textContent = value; }
    }

    function badgeClassFor(status) {
        if (status === 'CRITICAL') { return 'badge badge-critical pulse-critical px-2 py-1'; }
        if (status === 'WARNING') { return 'badge badge-warning px-2 py-1'; }
        if (status === 'HEALTHY') { return 'badge badge-healthy px-2 py-1'; }
        return 'badge badge-unknown px-2 py-1';
    }

    function authHeaders(extra) {
        var headers = {};
        var token = window.sessionStorage ? window.sessionStorage.getItem(TOKEN_KEY) : null;
        if (token) { headers.Authorization = 'Bearer ' + token; }
        Object.keys(extra || {}).forEach(function (k) { headers[k] = extra[k]; });
        return headers;
    }

    function requireSignIn() {
        stopPolling();
        setConnectionState('is-down', 'Not signed in (or session expired) - sign in at /app/ '
            + 'in this tab, then return to this page');
    }

    function getJson(url, options) {
        options = options || {};
        options.headers = authHeaders(options.headers);
        return fetch(url, options).then(function (response) {
            if (response.status === 401) { requireSignIn(); }
            return response.json().then(function (body) {
                if (!response.ok) {
                    var error = new Error(body && body.message ? body.message : 'Request failed');
                    error.status = response.status;
                    error.body = body;
                    throw error;
                }
                return body;
            }).catch(function (parseError) {
                if (parseError.status) { throw parseError; }
                throw new Error('HTTP ' + response.status);
            });
        });
    }

    // --------------------------------------------------------------- charts
    function initCharts() {
        var trendCanvas = document.getElementById('realtimeChart');
        var riskCanvas = document.getElementById('riskDistributionChart');
        if (!trendCanvas || !riskCanvas || typeof Chart === 'undefined') { return; }

        realtimeChart = new Chart(trendCanvas.getContext('2d'), {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: METRIC_LABELS[activeMetric],
                    data: [],
                    borderColor: '#f59e0b',
                    backgroundColor: 'rgba(245, 158, 11, 0.12)',
                    fill: true,
                    tension: 0.35,
                    pointRadius: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#9ca3af', maxTicksLimit: 8 } },
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#9ca3af' } }
                }
            }
        });

        riskChart = new Chart(riskCanvas.getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: ['Healthy', 'Warning', 'Critical'],
                datasets: [{
                    data: [0, 0, 0],
                    backgroundColor: ['#10b981', '#f59e0b', '#ef4444'],
                    borderWidth: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: { legend: { display: false } },
                cutout: '70%'
            }
        });
    }

    function updateCharts(records) {
        if (realtimeChart) {
            var ordered = records.slice(0, CHART_POINTS).reverse();
            realtimeChart.data.labels = ordered.map(function (record) {
                var stamp = record.timestamp ? String(record.timestamp) : '';
                return stamp.length > 11 ? stamp.substring(11) : stamp;
            });
            realtimeChart.data.datasets[0].label = METRIC_LABELS[activeMetric];
            realtimeChart.data.datasets[0].data = ordered.map(function (record) {
                var value = record[activeMetric];
                return (value === null || value === undefined) ? null : Number(value);
            });
            realtimeChart.update('none');
        }
    }

    // --------------------------------------------------------------- render
    function renderSummary(summary) {
        if (!summary) { return; }
        setText('card-total-hives', summary.active_hives === null || summary.active_hives === undefined
            ? EM_DASH : String(summary.active_hives));
        setText('card-avg-temp', formatNumber(summary.avg_temperature, 1, ' °C'));
        setText('card-avg-weight', formatNumber(summary.avg_weight, 1, ' kg'));
        setText('card-health-score', formatNumber(summary.overall_health_percent, 1, ' %'));

        var counts = summary.status_counts || {};
        setText('legend-healthy', formatText(counts.HEALTHY));
        setText('legend-warning', formatText(counts.WARNING));
        setText('legend-critical', formatText(counts.CRITICAL));

        if (riskChart) {
            riskChart.data.datasets[0].data = [
                counts.HEALTHY || 0, counts.WARNING || 0, counts.CRITICAL || 0
            ];
            riskChart.update('none');
        }
    }

    function buildTelemetryRow(record) {
        var row = document.createElement('tr');

        function cell(text, className) {
            var td = document.createElement('td');
            td.textContent = text;
            if (className) { td.className = className; }
            row.appendChild(td);
            return td;
        }

        cell(formatText(record.hive_id), 'fw-bold text-light');
        cell(formatText(record.timestamp), 'text-secondary hc-mono');
        cell(formatNumber(record.temperature, 1, ' °C'));
        cell(formatNumber(record.humidity, 1, ' %'));
        cell(formatNumber(record.weight, 1, ' kg'));
        cell(formatNumber(record.audio_frequency, 0, ' Hz'));
        cell(formatNumber(record.battery_level, 0, ' %'));

        var statusCell = document.createElement('td');
        var badge = document.createElement('span');
        badge.className = badgeClassFor(record.health_status);
        badge.textContent = formatText(record.health_status);
        if (record.reason) { badge.title = String(record.reason); }
        statusCell.appendChild(badge);
        row.appendChild(statusCell);

        return row;
    }

    function renderTelemetry(records) {
        if (!Array.isArray(records)) { return; }
        telemetryRecords = records;

        setText('record-count', records.length + (records.length === 1 ? ' record' : ' records'));

        var tbody = document.getElementById('telemetry-table-body');
        if (!tbody) { return; }
        tbody.textContent = '';

        if (records.length === 0) {
            var emptyRow = document.createElement('tr');
            var emptyCell = document.createElement('td');
            emptyCell.colSpan = 8;
            emptyCell.className = 'text-center text-secondary py-4';
            emptyCell.textContent = 'No telemetry recorded yet. Start the hardware simulator to populate the feed.';
            emptyRow.appendChild(emptyCell);
            tbody.appendChild(emptyRow);
            updateCharts([]);
            return;
        }

        var fragment = document.createDocumentFragment();
        records.forEach(function (record) { fragment.appendChild(buildTelemetryRow(record)); });
        tbody.appendChild(fragment);

        updateCharts(records);
        updateCriticalBanner(records);
    }

    function updateCriticalBanner(records) {
        var banner = document.getElementById('critical-banner');
        var textNode = document.getElementById('critical-banner-text');
        if (!banner || !textNode) { return; }

        var critical = records.filter(function (record) { return record.health_status === 'CRITICAL'; });
        if (critical.length === 0) {
            banner.classList.add('d-none');
            banner.classList.remove('d-flex');
            return;
        }

        var hives = [];
        critical.forEach(function (record) {
            if (record.hive_id && hives.indexOf(record.hive_id) === -1) { hives.push(record.hive_id); }
        });

        textNode.textContent = 'CRITICAL condition on ' + hives.join(', ') +
            ' — ' + formatText(critical[0].reason);
        banner.classList.remove('d-none');
        banner.classList.add('d-flex');
    }

    function upsertRecord(record) {
        if (!record) { return; }
        var merged = [record].concat(telemetryRecords);
        renderTelemetry(merged.slice(0, 20));
    }

    // ------------------------------------------------------- connection UI
    function setConnectionState(state, message) {
        var dot = document.getElementById('live-dot');
        if (dot) { dot.className = 'hc-live-dot ' + state; }
        setText('connection-status', message);
    }

    // ---------------------------------------------------------- transports
    function stopPolling() {
        if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function startPolling() {
        if (pollTimer !== null) { return; }   // never create a second timer
        refreshAll();
        pollTimer = setInterval(refreshAll, POLL_INTERVAL_MS);
    }

    // Live updates: every telemetry endpoint now requires authentication
    // (Phase 3). EventSource cannot send an Authorization header, so the
    // dashboard refreshes by authenticated polling instead of the SSE stream.
    function startLiveUpdates() {
        setConnectionState('is-polling', 'Live updates by authenticated polling every 3 s');
        startPolling();
    }

    // ------------------------------------------------------------ data load
    function fetchSummary() {
        return getJson('/api/summary')
            .then(renderSummary)
            .catch(function () { /* transient; the next tick retries */ });
    }

    function fetchTelemetry() {
        return getJson('/api/telemetry/latest')
            .then(renderTelemetry)
            .catch(function () { /* transient; the next tick retries */ });
    }

    function refreshAll() {
        fetchTelemetry();
        fetchSummary();
    }

    function loadBatches() {
        getJson('/api/batches').then(function (batches) {
            var tbody = document.getElementById('harvest-table-body');
            if (!tbody) { return; }
            tbody.textContent = '';

            if (!batches.length) {
                var row = document.createElement('tr');
                var cell = document.createElement('td');
                cell.colSpan = 8;
                cell.className = 'text-center text-secondary py-4';
                cell.textContent = 'No batches recorded yet. Use “Register Batch” to mint the first block.';
                row.appendChild(cell);
                tbody.appendChild(row);
                return;
            }

            batches.forEach(function (batch) {
                var row = document.createElement('tr');

                function push(text, className) {
                    var td = document.createElement('td');
                    td.textContent = text;
                    if (className) { td.className = className; }
                    row.appendChild(td);
                }

                push(formatText(batch.block_index), 'text-secondary');

                var linkCell = document.createElement('td');
                var link = document.createElement('a');
                link.href = '/verify/' + encodeURIComponent(batch.batch_id);
                link.target = '_blank';
                link.rel = 'noopener';
                link.className = 'text-warning fw-semibold text-decoration-none';
                link.textContent = formatText(batch.batch_id);
                linkCell.appendChild(link);
                row.appendChild(linkCell);

                push(formatText(batch.harvest_date));
                push(formatText(batch.hive_id));
                push(formatNumber(batch.total_weight, 1, ' kg'));
                push(formatNumber(batch.quality_score, 1, ' / 100'));

                var hashCell = document.createElement('td');
                hashCell.className = 'hc-mono text-secondary';
                hashCell.textContent = batch.block_hash
                    ? String(batch.block_hash).substring(0, 16) + '…' : EM_DASH;
                hashCell.title = formatText(batch.block_hash);
                row.appendChild(hashCell);

                var qrCell = document.createElement('td');
                if (batch.qr_path) {
                    var qrLink = document.createElement('a');
                    qrLink.href = '/static/' + batch.qr_path;
                    qrLink.target = '_blank';
                    qrLink.rel = 'noopener';
                    qrLink.className = 'btn btn-sm btn-outline-warning';
                    qrLink.textContent = 'PNG';
                    qrCell.appendChild(qrLink);
                } else {
                    qrCell.textContent = EM_DASH;
                }
                row.appendChild(qrCell);

                tbody.appendChild(row);
            });
        }).catch(function () { /* leave the previous table contents in place */ });

        getJson('/api/chain').then(function (chain) {
            setText('chain-status', 'Chain: ' + chain.height + ' block(s) · ' +
                (chain.valid ? 'valid' : 'INTEGRITY FAILURE'));
        }).catch(function () { /* leave as-is */ });
    }

    function loadMlStatus() {
        getJson('/api/ml/status').then(function (status) {
            setText('ml-engine-label', status.available
                ? 'ML: IsolationForest active'
                : 'ML: rule engine fallback');

            var list = document.getElementById('ml-status-list');
            if (list) {
                list.textContent = '';
                var metadata = status.metadata || {};
                var rows = [
                    ['Detector state', status.available ? 'Loaded and scoring' : 'Unavailable — rules only'],
                    ['Model type', formatText(status.model_type)],
                    ['Feature columns', (status.feature_columns || []).join(', ') || EM_DASH],
                    ['Model version', formatText(status.model_version)],
                    ['Training data', formatText(status.training_data_source) +
                        (status.real_world_data_used === false ? ' (no real-world data used)' : '')],
                    ['scikit-learn (trained / runtime)', formatText((status.compatibility || {}).trained_with_scikit_learn) +
                        ' / ' + formatText((status.compatibility || {}).runtime_scikit_learn)],
                    ['Max escalation by ML', formatText((status.fusion_policy || {}).ml_max_escalation)],
                    ['Trained at', formatText(metadata.trained_at)],
                    ['Training rows', formatText((metadata.provenance || {}).rows)],
                    ['Real-world evaluation', status.real_world_evaluation ? 'available' : 'none yet']
                ];
                if (status.error) { rows.push(['Reason unavailable', formatText(status.error)]); }

                rows.forEach(function (pair) {
                    var item = document.createElement('li');
                    item.className = 'list-group-item hc-list-item';
                    var label = document.createElement('span');
                    label.textContent = pair[0];
                    var value = document.createElement('strong');
                    value.textContent = pair[1];
                    item.appendChild(label);
                    item.appendChild(value);
                    list.appendChild(item);
                });
            }

            var metrics = document.getElementById('ml-metrics');
            if (metrics) {
                metrics.textContent = '';
                // Phase 7: these figures are measured on SYNTHETIC data from the
                // same generator used for training -- labelled as such, never as
                // real-world accuracy.
                var synthetic = status.synthetic_evaluation || {};
                var detection = synthetic.held_out_detection_rate || {};
                var cards = [
                    ['Synthetic swarming (not real-world)', detection.swarming],
                    ['Synthetic overwintering (not real-world)', detection.overwintering_heat_loss],
                    ['Synthetic varroa (not real-world)', detection.varroa],
                    ['Synthetic false alarms (not real-world)', synthetic.held_out_false_alarm_rate]
                ];
                cards.forEach(function (pair) {
                    var column = document.createElement('div');
                    column.className = 'col-6 col-lg-3';
                    var box = document.createElement('div');
                    box.className = 'glass-card p-3 text-center h-100';
                    var value = document.createElement('h4');
                    value.className = 'fw-bold mb-1 text-warning';
                    value.textContent = (pair[1] === null || pair[1] === undefined)
                        ? EM_DASH : (Number(pair[1]) * 100).toFixed(1) + ' %';
                    var caption = document.createElement('div');
                    caption.className = 'small text-secondary';
                    caption.textContent = pair[0];
                    box.appendChild(value);
                    box.appendChild(caption);
                    column.appendChild(box);
                    metrics.appendChild(column);
                });
            }
        }).catch(function () {
            setText('ml-engine-label', 'ML: status unavailable');
        });
    }

    function loadHiveOptions() {
        getJson('/api/hives').then(function (hives) {
            var select = document.getElementById('harvestHiveId');
            if (!select) { return; }
            var previous = select.value;
            select.textContent = '';

            if (!hives.length) {
                var placeholder = document.createElement('option');
                placeholder.textContent = 'No hives have reported telemetry yet';
                placeholder.value = '';
                select.appendChild(placeholder);
                return;
            }

            hives.forEach(function (hive) {
                var option = document.createElement('option');
                option.value = hive.hive_id;
                option.textContent = hive.hive_id;   // textContent: never innerHTML
                select.appendChild(option);
            });
            if (previous) { select.value = previous; }
        }).catch(function () { /* keep the existing options */ });
    }

    // ------------------------------------------------------------- harvest
    function submitHarvest() {
        var feedback = document.getElementById('harvest-feedback');
        var button = document.getElementById('btn-submit-harvest');
        var hiveId = (document.getElementById('harvestHiveId') || {}).value;

        function showFeedback(message, isError) {
            if (!feedback) { return; }
            feedback.textContent = message;               // textContent, not innerHTML
            feedback.className = 'small ' + (isError ? 'text-danger' : 'text-success');
        }

        if (!hiveId) {
            showFeedback('Select a hive that has reported telemetry first.', true);
            return;
        }

        var payload = {
            hive_id: hiveId,
            total_weight: parseFloat((document.getElementById('harvestWeight') || {}).value),
            floral_type: (document.getElementById('harvestFloral') || {}).value,
            harvest_date: (document.getElementById('harvestDate') || {}).value
        };
        var batchId = (document.getElementById('harvestBatchId') || {}).value;
        if (batchId && batchId.trim()) { payload.batch_id = batchId.trim(); }

        if (button) { button.disabled = true; }
        showFeedback('Computing quality score and minting the ledger block…', false);

        getJson('/api/harvest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(function (result) {
            showFeedback(
                'Batch ' + result.batch.batch_id + ' committed — quality ' +
                formatNumber(result.batch.quality_score, 1) + '/100, block #' +
                result.batch.block_index + '.', false);
            loadBatches();
            loadHiveOptions();
        }).catch(function (error) {
            showFeedback(error.message || 'Harvest registration failed.', true);
        }).then(function () {
            if (button) { button.disabled = false; }
        });
    }

    // ---------------------------------------------------------------- init
    document.addEventListener('DOMContentLoaded', function () {
        initCharts();

        var dateInput = document.getElementById('harvestDate');
        if (dateInput && !dateInput.value) {
            dateInput.value = new Date().toISOString().substring(0, 10);
        }

        var toggle = document.getElementById('metric-toggle');
        if (toggle) {
            toggle.addEventListener('click', function (event) {
                var button = event.target.closest('button[data-metric]');
                if (!button) { return; }
                activeMetric = button.getAttribute('data-metric');
                Array.prototype.forEach.call(
                    toggle.querySelectorAll('button'),
                    function (other) { other.classList.remove('active'); }
                );
                button.classList.add('active');
                updateCharts(telemetryRecords);
            });
        }

        var refreshButton = document.getElementById('btn-refresh');
        if (refreshButton) {
            refreshButton.addEventListener('click', function () {
                refreshAll();
                loadBatches();
                loadMlStatus();
                loadHiveOptions();
            });
        }

        var harvestButton = document.getElementById('btn-submit-harvest');
        if (harvestButton) { harvestButton.addEventListener('click', submitHarvest); }

        var harvestModal = document.getElementById('harvestModal');
        if (harvestModal) {
            harvestModal.addEventListener('show.bs.modal', loadHiveOptions);
        }

        refreshAll();
        loadBatches();
        loadMlStatus();
        loadHiveOptions();
        startLiveUpdates();
    });

    window.addEventListener('beforeunload', function () {
        stopPolling();
    });
})();
