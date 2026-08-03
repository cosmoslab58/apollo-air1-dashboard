// Shared hand-drawn SVG chart renderers, loaded after common.js on the pages
// that chart (technical/indoor). Previously renderRowChart was byte-identical
// across both page scripts and renderChart/renderOverlayRowChart were one more
// copy each; this is their single home. Depends on bandVar/fmt/escapeHtml from
// common.js.
//
// Series are plotted by real timestamp (not array index) so sources sampled at
// different rates overlay correctly. The viewBox width is the element's own
// measured pixel width, not a fixed constant -- with CSS sizing the svg to
// width:100%/height:auto, a fixed viewBox would get scaled to fit the
// container, and that scale would apply to everything inside (fixed-px text,
// row heights). Measuring the real width and using it 1:1 keeps 1 unit = 1 CSS
// pixel, so text/stroke/row-height stay true size on every screen.

const CHART_H = 170, CHART_PAD = { l: 38, r: 4, t: 10, b: 20 };
const ROW_H = 58, ROW_PAD_TOP = 17, ROW_PAD_BOTTOM = 8;
// r leaves room for each lane's own scale (see rowScaleLabels). The single-
// series renderChart below has carried a labelled y-axis all along; the lane
// charts had none, so a spike told you something happened but never how big --
// and since every lane auto-scales to its own min/max, two lanes with the same
// visual amplitude could differ by orders of magnitude. Endpoint labels rather
// than renderChart's four gridlines: a 33px-tall lane has no room for gridlines
// that wouldn't read as noise.
const ROW_PAD = { l: 2, r: 46 };
const ROW_SCALE_GAP = 6;

function measureWidth(el) {
  return Math.max(el.clientWidth, 240);
}

function formatTick(v) {
  const abs = Math.abs(v);
  if (abs >= 100) return String(Math.round(v));
  if (abs >= 10) return String(Math.round(v * 10) / 10);
  return String(Math.round(v * 100) / 100);
}

function pathFor(points, tMin, tMax, vMin, vMax, W) {
  const xw = W - CHART_PAD.l - CHART_PAD.r;
  const yh = CHART_H - CHART_PAD.t - CHART_PAD.b;
  return points.map((p, i) => {
    const x = CHART_PAD.l + ((p.t - tMin) / (tMax - tMin || 1)) * xw;
    const y = CHART_PAD.t + yh - ((p.v - vMin) / (vMax - vMin || 1)) * yh;
    return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function pointAt(p, tMin, tMax, vMin, vMax, W) {
  const xw = W - CHART_PAD.l - CHART_PAD.r;
  const yh = CHART_H - CHART_PAD.t - CHART_PAD.b;
  const x = CHART_PAD.l + ((p.t - tMin) / (tMax - tMin || 1)) * xw;
  const y = CHART_PAD.t + yh - ((p.v - vMin) / (vMax - vMin || 1)) * yh;
  return [x, y];
}

// Averages points into fixed-duration buckets sized to at least
// MIN_BUCKET_MS, and wider still if that's narrower than
// ~targetPxPerPoint screen pixels' worth of the chart's own time range --
// so a densely-sampled series (the AIR-1 reporting roughly once a minute)
// doesn't render every small real fluctuation as its own visible zigzag.
// The pixel-based half alone isn't enough when the chart's actual time
// span is itself short (e.g. a single-series chart whose axis is just
// that series' own ~2-hours-old history, not the nominal 24h range the
// range toggle implies) -- each pixel already represents very little
// real time then, so a point roughly a minute apart barely lands in the
// same bucket as its neighbor. The fixed floor guarantees real smoothing
// regardless of how much time is actually on screen; the pixel-based
// term takes over (and floors don't matter) once the axis is wide enough
// that it would call for a wider bucket anyway. A sparser series is
// unaffected either way: each of its points already lands in its own
// bucket. Only for line geometry -- callers keep the true latest raw
// point for any endpoint marker/label, so the displayed "current
// reading" is never a smoothed approximation.
const MIN_BUCKET_MS = 3 * 60000;
function downsampleForDisplay(points, tMin, tMax, pxWidth, targetPxPerPoint = 3) {
  if (points.length < 3) return points;
  const bucketMs = Math.max(((tMax - tMin) / pxWidth) * targetPxPerPoint, MIN_BUCKET_MS);
  if (!(bucketMs > 0)) return points;
  const out = [];
  let bucketStart = points[0].t, sumT = 0, sumV = 0, count = 0;
  for (const p of points) {
    if (p.t - bucketStart > bucketMs && count > 0) {
      out.push({ t: sumT / count, v: sumV / count });
      bucketStart = p.t;
      sumT = 0; sumV = 0; count = 0;
    }
    sumT += p.t; sumV += p.v; count++;
  }
  if (count > 0) out.push({ t: sumT / count, v: sumV / count });
  return out;
}

// One lane's y-scale, annotated at the highest and lowest values it actually
// plots. Shared by both lane renderers so a stacked chart and an Inside/Outside
// overlay are annotated identically.
//
// vMin/vMax, deliberately, not the lo/hi the line is drawn against: those carry
// the 12% headroom padding, which is a drawing device rather than a
// measurement, and labelling it puts numbers on screen that were never read --
// a CO2 lane whose real span was 590-6500ppm came out claiming -467 at the
// bottom. Each label sits at its own value's y position (so it is inset from
// the lane edge by exactly that padding), which is what makes it an annotation
// of the data rather than of the drawing.
function rowScaleLabels(yAt, vMin, vMax, decimals, W) {
  const x = (W - ROW_PAD.r + ROW_SCALE_GAP).toFixed(1);
  // Baseline sits at the text's bottom, so +4 centres a ~11px label on the
  // point it marks rather than hanging it above.
  const label = (v) => `<text class="chart-axis-label chart-y-label" x="${x}" y="${(yAt(v) + 4).toFixed(1)}">${fmt(v, decimals)}</text>`;
  // A dead-flat lane has one value, not a range -- two identical labels at the
  // same y would just overprint each other.
  return vMax === vMin ? label(vMax) : label(vMax) + label(vMin);
}

// series: [{ color, points: [{t, v}], area }] -- one shared y-scale, grid +
// y-axis labels. Used for the single-series indoor charts (temp/humidity).
function renderChart(el, series, opts) {
  const nonEmpty = series.filter((s) => s.points.length > 0);
  if (nonEmpty.length === 0) {
    el.innerHTML = '<div class="empty-state">No data in this range yet.</div>';
    return;
  }
  const W = measureWidth(el);
  const allTimes = nonEmpty.flatMap((s) => s.points.map((p) => p.t));
  const allVals = nonEmpty.flatMap((s) => s.points.map((p) => p.v));
  const tMin = Math.min(...allTimes), tMax = Math.max(...allTimes);
  const vMin = Math.min(...allVals), vMax = Math.max(...allVals);
  const pad = (vMax - vMin) * 0.12 || 1;
  const lo = vMin - pad;
  const hi = vMax + pad;

  let svg = `<svg viewBox="0 0 ${W} ${CHART_H}" preserveAspectRatio="none" role="img" aria-label="${opts.label || "chart"}">`;
  for (let gy = 0; gy <= 3; gy++) {
    const y = CHART_PAD.t + (gy / 3) * (CHART_H - CHART_PAD.t - CHART_PAD.b);
    svg += `<line class="chart-grid-line" x1="${CHART_PAD.l}" y1="${y.toFixed(1)}" x2="${W - CHART_PAD.r}" y2="${y.toFixed(1)}" />`;
    const tickVal = hi - (gy / 3) * (hi - lo);
    svg += `<text class="chart-axis-label chart-y-label" x="${(CHART_PAD.l - 6).toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="end">${formatTick(tickVal)}</text>`;
  }
  nonEmpty.forEach((s) => {
    const plotPoints = downsampleForDisplay(s.points, tMin, tMax, W - CHART_PAD.l - CHART_PAD.r);
    const d = pathFor(plotPoints, tMin, tMax, lo, hi, W);
    if (s.area) {
      const yh = CHART_H - CHART_PAD.t - CHART_PAD.b;
      const areaD = `${d} L${(W - CHART_PAD.r).toFixed(1)},${(CHART_PAD.t + yh).toFixed(1)} L${CHART_PAD.l},${(CHART_PAD.t + yh).toFixed(1)} Z`;
      svg += `<path d="${areaD}" fill="${s.color}" opacity="0.12" stroke="none" />`;
    }
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`;
    const last = s.points[s.points.length - 1];
    const [ex, ey] = pointAt(last, tMin, tMax, lo, hi, W);
    svg += `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.5" fill="${s.color}" stroke="var(--panel-raised)" stroke-width="1.5" />`;
  });
  svg += `<text class="chart-axis-label" x="${CHART_PAD.l}" y="${CHART_H - 4}">${opts.leftLabel || ""}</text>`;
  svg += `<text class="chart-axis-label" x="${W - CHART_PAD.r}" y="${CHART_H - 4}" text-anchor="end">now</text>`;
  svg += "</svg>";
  el.innerHTML = svg;
}

// One SVG, N horizontal lanes -- all rows share the same time axis (drawn once,
// at the bottom) but each gets its own y-scale sized to its own min/max, so a
// small-magnitude series is never squashed flat by a big one. Color tracks
// severity, per point, via the row's own bandFor(value).
// rows: [{ label, unit, decimals, points: [{t, v}], bandFor(v) => band|null }]
function renderRowChart(el, rows, opts) {
  const nonEmpty = rows.filter((r) => r.points.length > 0);
  if (nonEmpty.length === 0) {
    el.innerHTML = '<div class="empty-state">No data in this range yet.</div>';
    return;
  }
  const W = measureWidth(el);
  const allTimes = nonEmpty.flatMap((r) => r.points.map((p) => p.t));
  const tMin = Math.min(...allTimes), tMax = Math.max(...allTimes);
  const totalH = nonEmpty.length * ROW_H + 14;

  let svg = `<svg viewBox="0 0 ${W} ${totalH}" preserveAspectRatio="none" role="img" aria-label="${opts.label || "chart"}">`;
  nonEmpty.forEach((r, i) => {
    const top = i * ROW_H;
    if (i > 0) {
      svg += `<line class="chart-grid-line" x1="${ROW_PAD.l}" y1="${top.toFixed(1)}" x2="${W - ROW_PAD.r}" y2="${top.toFixed(1)}" />`;
    }
    const dotY = top + ROW_PAD_TOP - 8;
    const labelY = top + ROW_PAD_TOP - 5;
    const vals = r.points.map((p) => p.v);
    const vMin = Math.min(...vals), vMax = Math.max(...vals);
    const pad = (vMax - vMin) * 0.12 || 1;
    const lo = vMin - pad, hi = vMax + pad;
    const xw = W - ROW_PAD.l - ROW_PAD.r;
    const yh = ROW_H - ROW_PAD_TOP - ROW_PAD_BOTTOM;
    const xAt = (t) => ROW_PAD.l + ((t - tMin) / (tMax - tMin || 1)) * xw;
    const yAt = (v) => top + ROW_PAD_TOP + yh - ((v - lo) / (hi - lo || 1)) * yh;

    const plotPoints = downsampleForDisplay(r.points, tMin, tMax, xw);
    for (let j = 1; j < plotPoints.length; j++) {
      const p0 = plotPoints[j - 1], p1 = plotPoints[j];
      const segColor = bandVar(r.bandFor(p1.v));
      svg += `<path d="M${xAt(p0.t).toFixed(1)},${yAt(p0.v).toFixed(1)} L${xAt(p1.t).toFixed(1)},${yAt(p1.v).toFixed(1)}" fill="none" stroke="${segColor}" stroke-width="2" stroke-linecap="round" />`;
    }

    const last = r.points[r.points.length - 1];
    const lastColor = bandVar(r.bandFor(last.v));
    const ex = xAt(last.t), ey = yAt(last.v);
    svg += `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.5" fill="${lastColor}" stroke="var(--panel-raised)" stroke-width="1.5" />`;
    svg += `<circle cx="${(ROW_PAD.l + 3).toFixed(1)}" cy="${dotY.toFixed(1)}" r="3" fill="${lastColor}" />`;
    svg += rowScaleLabels(yAt, vMin, vMax, r.decimals, W);
    svg += `<text class="chart-axis-label" x="${(ROW_PAD.l + 10).toFixed(1)}" y="${labelY.toFixed(1)}">${escapeHtml(r.label)} <tspan style="fill:${lastColor}">${fmt(last.v, r.decimals)}${r.unit}</tspan></text>`;
  });
  const bottomY = nonEmpty.length * ROW_H + 10;
  svg += `<text class="chart-axis-label" x="${ROW_PAD.l}" y="${bottomY}">${opts.leftLabel || ""}</text>`;
  svg += `<text class="chart-axis-label" x="${W - ROW_PAD.r}" y="${bottomY}" text-anchor="end">now</text>`;
  svg += "</svg>";
  el.innerHTML = svg;
}

// Same idea as renderRowChart, but each row overlays two series (Inside and
// Outside) sharing one y-scale within that row. Color still tracks severity per
// point on each line; Inside/Outside identity comes from line weight (Inside
// faded, Outside full-strength -- a dashed line was tried first, but a dash
// pattern restarts at the start of every short per-segment <path>, and real
// sensor noise rarely produces segments long enough to show more than a
// solid-looking stroke -- opacity doesn't care how short or jagged the
// segments are) and the "In"/"Out" label prefix.
// rows: [{ label, unit, decimals, inside: {points, bandFor}, outside: {points, bandFor} }]
function renderOverlayRowChart(el, rows, opts) {
  const nonEmpty = rows.filter((r) => r.inside.points.length > 0 || r.outside.points.length > 0);
  if (nonEmpty.length === 0) {
    el.innerHTML = '<div class="empty-state">No data in this range yet.</div>';
    return;
  }
  const W = measureWidth(el);
  const allTimes = nonEmpty.flatMap((r) => [...r.inside.points, ...r.outside.points].map((p) => p.t));
  const tMin = Math.min(...allTimes), tMax = Math.max(...allTimes);
  const totalH = nonEmpty.length * ROW_H + 14;

  let svg = `<svg viewBox="0 0 ${W} ${totalH}" preserveAspectRatio="none" role="img" aria-label="${opts.label || "chart"}">`;
  nonEmpty.forEach((r, i) => {
    const top = i * ROW_H;
    if (i > 0) {
      svg += `<line class="chart-grid-line" x1="${ROW_PAD.l}" y1="${top.toFixed(1)}" x2="${W - ROW_PAD.r}" y2="${top.toFixed(1)}" />`;
    }
    const combined = [...r.inside.points, ...r.outside.points];
    const vals = combined.map((p) => p.v);
    const vMin = Math.min(...vals), vMax = Math.max(...vals);
    const pad = (vMax - vMin) * 0.12 || 1;
    const lo = vMin - pad, hi = vMax + pad;
    const xw = W - ROW_PAD.l - ROW_PAD.r;
    const yh = ROW_H - ROW_PAD_TOP - ROW_PAD_BOTTOM;
    const xAt = (t) => ROW_PAD.l + ((t - tMin) / (tMax - tMin || 1)) * xw;
    const yAt = (v) => top + ROW_PAD_TOP + yh - ((v - lo) / (hi - lo || 1)) * yh;

    const drawSeries = (series, isInside) => {
      // Opacity goes on a wrapping <g>, not each segment's own <path> --
      // every point is its own short path with round line caps, so at
      // real sample density the caps of adjacent segments overlap right
      // at each shared point. Per-path opacity compounds where shapes
      // overlap (two stacked 50% layers blend to ~75%), so every sample
      // point was rendering as a near-solid dot, and with enough points
      // close together the whole line looked solid again. A <g> composites
      // its children as one flattened layer first, then fades that layer
      // once -- overlaps inside the group stay full-strength relative to
      // each other (invisible anyway, same color), only the one group-to-
      // background blend is at 50%.
      const plotPoints = downsampleForDisplay(series.points, tMin, tMax, xw);
      if (isInside) svg += '<g opacity="0.5">';
      for (let j = 1; j < plotPoints.length; j++) {
        const p0 = plotPoints[j - 1], p1 = plotPoints[j];
        const segColor = bandVar(series.bandFor(p1.v));
        svg += `<path d="M${xAt(p0.t).toFixed(1)},${yAt(p0.v).toFixed(1)} L${xAt(p1.t).toFixed(1)},${yAt(p1.v).toFixed(1)}" fill="none" stroke="${segColor}" stroke-width="2" stroke-linecap="round" />`;
      }
      if (isInside) svg += '</g>';
      if (series.points.length === 0) return null;
      const last = series.points[series.points.length - 1];
      const color = bandVar(series.bandFor(last.v));
      const ex = xAt(last.t), ey = yAt(last.v);
      // Open ring for Inside, filled dot for Outside -- the endpoint marker
      // stays full-strength even though the trailing line fades, so the
      // current reading is always the most legible part of either series.
      if (isInside) {
        svg += `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.5" fill="var(--panel-raised)" stroke="${color}" stroke-width="2" />`;
      } else {
        svg += `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.5" fill="${color}" stroke="var(--panel-raised)" stroke-width="1.5" />`;
      }
      return last;
    };

    const lastIn = drawSeries(r.inside, true);
    const lastOut = drawSeries(r.outside, false);
    // One scale per lane here too -- Inside and Outside share it, which is the
    // point of overlaying them, so a single pair of labels covers both.
    svg += rowScaleLabels(yAt, vMin, vMax, r.decimals, W);
    const inColor = lastIn ? bandVar(r.inside.bandFor(lastIn.v)) : "var(--ink-dim)";
    const outColor = lastOut ? bandVar(r.outside.bandFor(lastOut.v)) : "var(--ink-dim)";
    const inText = lastIn ? `In ${fmt(lastIn.v, r.decimals)}${r.unit}` : "In —";
    const outText = lastOut ? `Out ${fmt(lastOut.v, r.decimals)}${r.unit}` : "Out —";
    const labelY = top + ROW_PAD_TOP - 5;
    svg += `<text class="chart-axis-label" x="${ROW_PAD.l.toFixed(1)}" y="${labelY.toFixed(1)}">${escapeHtml(r.label)} <tspan style="fill:${inColor}">${inText}</tspan> · <tspan style="fill:${outColor}">${outText}</tspan></text>`;
  });
  const bottomY = nonEmpty.length * ROW_H + 10;
  svg += `<text class="chart-axis-label" x="${ROW_PAD.l}" y="${bottomY}">${opts.leftLabel || ""}</text>`;
  svg += `<text class="chart-axis-label" x="${W - ROW_PAD.r}" y="${bottomY}" text-anchor="end">now</text>`;
  svg += "</svg>";
  el.innerHTML = svg;
}
