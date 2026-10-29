/**
 * Mini payoff chart for grouped option positions on the Positions page.
 * Shows expiry payoff curve for the combined legs in a position group.
 */
import type * as PlotlyTypes from 'plotly.js'
import { useMemo } from 'react'
import Plot from '@/lib/Plot2D'
import {
  computePayoff,
  daysToExpiry,
  daysToYears,
  payoffPriceRange,
  type OptionType,
  type StrategyLeg,
} from '@/lib/strategyMath'
import { useThemeStore } from '@/stores/themeStore'
import type { Position } from '@/types/trading'

interface PositionPayoffChartProps {
  positions: Position[]
  spot: number
  height?: number
  formatCurrency: (value: number) => string
}

/** Parse option symbol to extract strike and option type */
function parseOptionSymbol(symbol: string): { strike: number; optionType: OptionType } | null {
  // Format: BASE[DDMMMYY][STRIKE][CE|PE] e.g. NIFTY28APR2622000CE
  const match = symbol.match(/(\d+(?:\.\d+)?)(CE|PE)$/i)
  if (!match) return null
  return {
    strike: parseFloat(match[1]),
    optionType: match[2].toUpperCase() as OptionType,
  }
}

/** Parse expiry from symbol */
function parseExpiry(symbol: string): string | null {
  // Format: BASE[DDMMMYY]... e.g. NIFTY28APR26...
  const match = symbol.match(/(\d{1,2}[A-Z]{3}\d{2})/i)
  return match ? match[1].toUpperCase() : null
}

/** Convert positions to strategy legs for payoff calculation */
function positionsToLegs(positions: Position[], spot: number): StrategyLeg[] {
  return positions
    .filter((p) => {
      const qty = Number(p.quantity) || 0
      return qty !== 0 && (p.exchange === 'NFO' || p.exchange === 'BFO' || p.exchange === 'MCX')
    })
    .map((p, idx) => {
      const parsed = parseOptionSymbol(p.symbol)
      const expiry = parseExpiry(p.symbol) || ''
      const qty = Number(p.quantity) || 0
      const lotSize = p.lot_size || 1

      return {
        id: `pos-${idx}`,
        segment: parsed ? 'OPTION' : 'FUTURE',
        side: qty > 0 ? 'BUY' : 'SELL',
        lots: Math.abs(qty) / lotSize,
        lotSize,
        expiry,
        strike: parsed?.strike,
        optionType: parsed?.optionType,
        price: Number(p.average_price) || 0,
        iv: 15, // Default IV for display
        active: true,
        symbol: p.symbol,
        exchange: p.exchange,
        referenceUnderlying: spot,
      } as StrategyLeg
    })
}

export function PositionPayoffChart({
  positions,
  spot,
  height = 200,
}: PositionPayoffChartProps) {
  const { mode, appMode } = useThemeStore()
  const isAnalyzer = appMode === 'analyzer'
  const isDark = mode === 'dark' || isAnalyzer

  const colors = useMemo(
    () => ({
      paper: isDark ? (isAnalyzer ? '#1a1530' : '#0f172a') : '#ffffff',
      bg: isDark ? (isAnalyzer ? '#221a3a' : '#1e293b') : '#f8fafc',
      text: isDark ? '#e2e8f0' : '#1e293b',
      profit: isDark ? 'rgba(34,197,94,0.3)' : 'rgba(34,197,94,0.25)',
      loss: isDark ? 'rgba(239,68,68,0.3)' : 'rgba(239,68,68,0.25)',
      expiryLine: isDark ? '#fb923c' : '#ea580c',
      zeroLine: isDark ? 'rgba(226,232,240,0.5)' : 'rgba(15,23,42,0.5)',
      spotLine: isDark ? '#f472b6' : '#db2777',
    }),
    [isDark, isAnalyzer]
  )

  const { data, layout, config, hasData } = useMemo(() => {
    const legs = positionsToLegs(positions, spot)
    if (legs.length === 0 || spot <= 0) {
      return { data: [], layout: {}, config: {}, hasData: false }
    }

    // Calculate days to nearest expiry
    const expiries = legs.map((l) => l.expiry).filter(Boolean)
    const daysToExp = expiries.length > 0 ? Math.min(...expiries.map((e) => daysToExpiry(e))) : 30
    const tYears = daysToYears(daysToExp)

    // Get price range
    const atmIv = 15
    const [lo, hi] = payoffPriceRange(spot, legs, atmIv, tYears)

    // Compute payoff
    const payoff = computePayoff(legs, spot, daysToExp, 0, [lo, hi], 100, 0, atmIv)

    if (payoff.samples.length === 0) {
      return { data: [], layout: {}, config: {}, hasData: false }
    }

    const xs = payoff.samples.map((s) => s.underlying)
    const ys = payoff.samples.map((s) => s.expiry)

    // Split into profit/loss fills
    const profitFill = payoff.samples.map((s) => (s.expiry >= 0 ? s.expiry : 0))
    const lossFill = payoff.samples.map((s) => (s.expiry < 0 ? s.expiry : 0))

    const traces: PlotlyTypes.Data[] = [
      {
        x: xs,
        y: profitFill,
        type: 'scatter',
        mode: 'none',
        fill: 'tozeroy',
        fillcolor: colors.profit,
        showlegend: false,
        hoverinfo: 'skip',
      },
      {
        x: xs,
        y: lossFill,
        type: 'scatter',
        mode: 'none',
        fill: 'tozeroy',
        fillcolor: colors.loss,
        showlegend: false,
        hoverinfo: 'skip',
      },
      {
        x: xs,
        y: ys,
        type: 'scatter',
        mode: 'lines',
        name: 'At Expiry',
        line: { color: colors.expiryLine, width: 2 },
        hovertemplate: 'Price: %{x:.2f}<br>P&L: %{y:.2f}<extra></extra>',
      },
    ]

    const shapes: Partial<PlotlyTypes.Shape>[] = [
      {
        type: 'line',
        xref: 'paper',
        x0: 0,
        x1: 1,
        yref: 'y',
        y0: 0,
        y1: 0,
        line: { color: colors.zeroLine, width: 1 },
      },
      {
        type: 'line',
        xref: 'x',
        x0: spot,
        x1: spot,
        yref: 'paper',
        y0: 0,
        y1: 1,
        line: { color: colors.spotLine, width: 1.5, dash: 'dot' },
      },
    ]

    const chartLayout: Partial<PlotlyTypes.Layout> = {
      paper_bgcolor: colors.paper,
      plot_bgcolor: colors.bg,
      font: { color: colors.text, size: 10 },
      hovermode: 'x unified',
      margin: { l: 50, r: 10, t: 10, b: 30 },
      showlegend: false,
      xaxis: {
        tickfont: { size: 9 },
        gridcolor: isDark ? 'rgba(148,163,184,0.1)' : 'rgba(15,23,42,0.05)',
        zeroline: false,
      },
      yaxis: {
        tickfont: { size: 9 },
        gridcolor: isDark ? 'rgba(148,163,184,0.1)' : 'rgba(15,23,42,0.05)',
        zeroline: false,
      },
      shapes,
    }

    return {
      data: traces,
      layout: chartLayout,
      config: {
        displayModeBar: false,
        responsive: true,
      } as Partial<PlotlyTypes.Config>,
      hasData: true,
    }
  }, [positions, spot, colors, isDark])

  if (!hasData) {
    return (
      <div
        className="flex items-center justify-center text-xs text-muted-foreground"
        style={{ height }}
      >
        No F&O positions for payoff
      </div>
    )
  }

  return <Plot data={data} layout={layout} config={config} useResizeHandler style={{ width: '100%', height }} />
}
