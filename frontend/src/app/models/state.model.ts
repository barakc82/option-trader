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

  export interface State {
    status: string;
    time: number;
    last_updated: string;
    spx_price: number | null;
    excess_liquidity: string;
    cash: number;
    cushion: number;
    target_delta: number;
    target_delta_increase: number;
    implied_volatility: number;
    risk_fraction: number;
    start_time: string;
    liquidation_alert_time: number;
    liquidation_time: number;
    margin_lock: string;
    last_put_option_price: number;
    last_call_option_price: number;
    margin_deficiency: number;
    put_options_above_minimal_sell_price: boolean;
    call_options_above_minimal_sell_price: boolean;
    positions: Position[];
    trades: Trade[];
    fills: Fill[];
  }