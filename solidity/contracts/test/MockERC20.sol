// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract MockERC20 is ERC20 {
    uint8 private _decimals;

    constructor(
        string memory name_,
        string memory symbol_,
        uint8 decimals_,
        uint256 premint_,
        address recipient
    ) ERC20(name_, symbol_) {
        _decimals = decimals_;
        _mint(recipient, premint_ * 10 ** decimals_);
    }

    function decimals() public view override returns (uint8) {
        return _decimals;
    }
}
