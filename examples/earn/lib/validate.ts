import { isAddress, isHex } from "viem";
import type { Address, Hex } from "viem";

const DECIMAL_AMOUNT = /^\d+$/;

export const ensureAddress = (value: unknown, field: string): Address => {
  if (typeof value !== "string" || !isAddress(value)) {
    throw new ValidationError(`${field} must be a valid address`);
  }
  return value;
};

export const ensureHex = (value: unknown, field: string, byteLen?: number): Hex => {
  if (typeof value !== "string" || !isHex(value)) {
    throw new ValidationError(`${field} must be 0x-prefixed hex`);
  }
  if (byteLen !== undefined && value.length !== 2 + byteLen * 2) {
    throw new ValidationError(`${field} must be ${byteLen} bytes`);
  }
  return value;
};

export const ensureAmountString = (value: unknown, field: string): string => {
  if (typeof value !== "string" || !DECIMAL_AMOUNT.test(value)) {
    throw new ValidationError(`${field} must be a non-negative integer string`);
  }
  return value;
};

export const ensureNonEmptyString = (value: unknown, field: string): string => {
  if (typeof value !== "string" || value.length === 0) {
    throw new ValidationError(`${field} is required`);
  }
  return value;
};

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}
