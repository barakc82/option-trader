export interface Position {
    right: string;
    strike: number;
    quantity: number;
    date: string;
    lastTradeDateOrContractMonth: string;
    delta: number;
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
  
  export interface State {
    status: string;
    time: string;
    positions: Position[];
    trades: Trade[];
  }