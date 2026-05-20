export interface Position {
    right: string;
    strike: number;
    quantity: number;
    date: string;
    last_price: string;
    delta: string;
    stop_loss: number;
  }
  
  export interface Trade {
    action: string;
    right: string;
    strike: number;
    quantity: number;
    date: string;
    delta: number;
    order_type: string;
    limit: number;
  }
  
  export interface Fill {
    action: string;
    right: string;
    strike: number;
    quantity: number;
    time: number;
    price: number;
    comment: string;
  }

  export interface MarginReductionDetails {
    option: string;
    margin_deficiency: number;
    margin_change: number;
    required_level: number;
  }

  export interface State {
    status: string;
    time: number;
    last_updated: string;
    spx_price: number | null;
    excess_liquidity: string;
    cash: number;
    cushion: number;
    call_target_delta: number;
    put_target_delta: number;
    call_target_delta_increase: number;
    put_target_delta_increase: number;
    call_implied_volatility: number;
    put_implied_volatility: number;
    risk_fraction: number;
    start_time: string;
    liquidation_alert_time: number;
    liquidation_time: number;
    margin_lock: string;
    last_put_option_price: number;
    last_call_option_price: number;
    call_margin_reduction: MarginReductionDetails | null;
    put_margin_reduction: MarginReductionDetails | null;
    put_options_above_minimal_sell_price: boolean;
    call_options_above_minimal_sell_price: boolean;
    positions: Position[];
    trades: Trade[];
    fills: Fill[];
  }