/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
package org.apache.fineract.portfolio.loanaccount.loanschedule.domain;

import java.math.BigDecimal;
import java.math.MathContext;
import java.time.LocalDate;
import java.util.Collection;
import java.util.Map;
import java.util.TreeMap;
import lombok.extern.slf4j.Slf4j;
import org.apache.fineract.infrastructure.core.exception.GeneralPlatformDomainRuleException;
import org.apache.fineract.infrastructure.core.service.DateUtils;
import org.apache.fineract.organisation.monetary.domain.Money;
import org.apache.fineract.organisation.monetary.mapper.CurrencyMapper;
import org.apache.fineract.portfolio.loanaccount.data.LoanTermVariationsData;
import org.apache.fineract.portfolio.loanaccount.domain.LoanTransactionRepository;
import org.apache.fineract.portfolio.loanaccount.loanschedule.service.BrokenPeriodMethodResolver;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

/**
 * Cumulative declining-balance schedule generator with broken-period interest on the first installment — a common NBFC
 * lending pattern.
 *
 * <p>
 * The loan keeps a plain-annuity EMI (which the stock cumulative generator already produces under
 * {@code SAME_AS_REPAYMENT_PERIOD}) but the <b>first</b> installment's interest is charged on the number of days
 * between the interest-start date (the {@code interestChargedFromDate} when given, else disbursement) and the first
 * repayment (the "broken period"), at a 30-day-month daily rate, rather than a full month. How the day count and the
 * installment amount are derived is a per-product choice, resolved from the
 * {@code loan_product_first_emi_config.broken_period_method} datatable column via {@link BrokenPeriodMethodResolver}:
 * </p>
 * <ul>
 * <li>{@code null} / {@code ACTUAL_DAYS_SURCHARGE} (Vivriti; null = legacy default): actual calendar days. Over 30 days
 * the excess interest is a surcharge on top of the regular EMI (principal stays on a 30-day basis); under 30 days the
 * installment stays = EMI and the saved interest moves to principal.</li>
 * <li>{@code ADJUSTED_DAYS_SYMMETRIC} (DMI): days normalized so the interest-start month counts as 30 days (31-day
 * month &rarr; &minus;1 day, February &rarr; +2). The principal always stays on a 30-day basis, so the installment
 * moves symmetrically: EMI + dailyRate&times;(adjustedDays&minus;30), smaller than the EMI for short broken
 * periods.</li>
 * <li>{@code STANDARD}: explicit opt-out, stock Fineract first period.</li>
 * </ul>
 *
 * <p>
 * Under {@code SAME_AS_REPAYMENT_PERIOD} the stock first-period interest equals
 * {@code outstandingBalance * monthlyRate} (a full 30-day month). The broken-period interest is
 * {@code outstandingBalance * (monthlyRate / 30) * brokenDays}. So this generator overrides <b>only</b> the first
 * period and leaves every other period (and all non-{@code SAME_AS_REPAYMENT_PERIOD} loans) to the stock behaviour via
 * {@code super}.
 * </p>
 *
 * <p>
 * Registered as {@link Primary} so it transparently replaces the stock cumulative declining-balance generator wherever
 * it is injected (e.g. {@code DefaultLoanScheduleGeneratorFactory}); the gate below makes it a no-op for everything
 * except a broken first period on a {@code SAME_AS_REPAYMENT_PERIOD} product, so other products are unaffected.
 * </p>
 */
@Slf4j
@Component
@Primary
public class BrokenPeriodDecliningBalanceLoanScheduleGenerator extends CumulativeDecliningBalanceInterestLoanScheduleGenerator {

    private static final int DAYS_IN_MONTH = 30;
    // Daily rate = annualRate% / 12 (months) / 100 (percent) / 30 (days) = annualRate / 36000.
    private static final BigDecimal DAILY_RATE_DIVISOR = BigDecimal.valueOf(36000);
    // Monthly rate = annualRate% / 12 / 100 = annualRate / 1200 (also the full 30-day-month interest factor).
    private static final BigDecimal MONTHLY_RATE_DIVISOR = BigDecimal.valueOf(1200);

    private final BrokenPeriodMethodResolver brokenPeriodMethodResolver;

    public BrokenPeriodDecliningBalanceLoanScheduleGenerator(final ScheduledDateGenerator scheduledDateGenerator,
            final PaymentPeriodsInOneYearCalculator paymentPeriodsInOneYearCalculator,
            final LoanTransactionRepository loanTransactionRepository, final CurrencyMapper currencyMapper,
            final BrokenPeriodMethodResolver brokenPeriodMethodResolver) {
        super(scheduledDateGenerator, paymentPeriodsInOneYearCalculator, loanTransactionRepository, currencyMapper);
        this.brokenPeriodMethodResolver = brokenPeriodMethodResolver;
    }

    @Override
    public PrincipalInterest calculatePrincipalInterestComponentsForPeriod(final PaymentPeriodsInOneYearCalculator calculator,
            final BigDecimal interestCalculationGraceOnRepaymentPeriodFraction, final Money totalCumulativePrincipal,
            final Money totalCumulativeInterest, final Money totalInterestDueForLoan, final Money cumulatingInterestPaymentDueToGrace,
            final Money outstandingBalance, final LoanApplicationTerms loanApplicationTerms, final int periodNumber, final MathContext mc,
            final TreeMap<LocalDate, Money> principalVariation, final Map<LocalDate, Money> compoundingMap, final LocalDate periodStartDate,
            final LocalDate periodEndDate, final Collection<LoanTermVariationsData> termVariations) {

        final PrincipalInterest stock = super.calculatePrincipalInterestComponentsForPeriod(calculator,
                interestCalculationGraceOnRepaymentPeriodFraction, totalCumulativePrincipal, totalCumulativeInterest,
                totalInterestDueForLoan, cumulatingInterestPaymentDueToGrace, outstandingBalance, loanApplicationTerms, periodNumber, mc,
                principalVariation, compoundingMap, periodStartDate, periodEndDate, termVariations);

        // Only the FIRST period of a plain-annuity (SAME_AS_REPAYMENT_PERIOD) loan deviates.
        if (periodNumber != 1 || !loanApplicationTerms.getInterestCalculationPeriodMethod().isSameAsRepaymentPeriod()) {
            return stock;
        }

        // Per-product method; null (no config row) keeps the legacy actual-days behaviour for compatibility.
        // Any OTHER value is a misconfigured datatable row: fail loudly rather than silently pricing the
        // schedule with the wrong lender's math.
        final String method = brokenPeriodMethodResolver.resolve(loanApplicationTerms.getLoanProductId());
        if (BrokenPeriodMethodResolver.STANDARD.equalsIgnoreCase(method)) {
            return stock;
        }
        final boolean adjustedSymmetric = BrokenPeriodMethodResolver.ADJUSTED_DAYS_SYMMETRIC.equalsIgnoreCase(method);
        if (method != null && !adjustedSymmetric && !BrokenPeriodMethodResolver.ACTUAL_DAYS_SURCHARGE.equalsIgnoreCase(method)) {
            throw new GeneralPlatformDomainRuleException("error.msg.loan.product.broken.period.method.unknown",
                    "Unknown broken_period_method '" + method + "' configured for loan product " + loanApplicationTerms.getLoanProductId()
                            + " in loan_product_first_emi_config",
                    method, loanApplicationTerms.getLoanProductId());
        }

        // Broken period = calendar days from the INTEREST-START date (interestChargedFromDate when given, else
        // disbursement) to the first repayment date. (Under SAME_AS_REPAYMENT the period start passed in is the
        // nominal month start, not the disbursal date.)
        final LocalDate interestStartDate = loanApplicationTerms.getInterestChargedFromLocalDate() != null
                ? loanApplicationTerms.getInterestChargedFromLocalDate()
                : loanApplicationTerms.getExpectedDisbursementDate();
        final int actualBrokenDays = DateUtils.getExactDifferenceInDays(interestStartDate, periodEndDate);

        // DMI-style day count normalizes the interest-start month to 30 days (31-day month -1, February +2).
        final int brokenDays = adjustedSymmetric ? actualBrokenDays - (interestStartDate.lengthOfMonth() - DAYS_IN_MONTH)
                : actualBrokenDays;
        if (brokenDays == DAYS_IN_MONTH) {
            return stock; // a full 30-day-equivalent month: stock already matches
        }

        // Broken-period interest = round(OB * dailyRate * brokenDays), dailyRate = annualRate% / 12 / 100 / 30
        // = annualRate / 36000. Compute from the EXACT balance & rate (single rounding) to avoid the double-rounding
        // that scaling the already-rounded stock interest would introduce.
        final BigDecimal dailyRate = loanApplicationTerms.getAnnualNominalInterestRate().divide(DAILY_RATE_DIVISOR, mc);
        final BigDecimal exactInterest = outstandingBalance.getAmount().multiply(dailyRate, mc).multiply(BigDecimal.valueOf(brokenDays),
                mc);
        final Money brokenInterest = Money.of(outstandingBalance.getCurrency(), exactInterest, mc);

        final Money emi = stock.principal().plus(stock.interest()); // plain-annuity installment (whole rupee)
        final Money newPrincipal;
        if (adjustedSymmetric || brokenDays > DAYS_IN_MONTH) {
            // Principal stays on a 30-day basis, so installment = EMI + dailyRate*(brokenDays-30). Applies to the
            // legacy/Vivriti long first period (surcharge) and to BOTH directions of the DMI symmetric method
            // (negative delta shrinks the first installment below the EMI).
            // The reference implementations compute round(EMI_unrounded - OB*dailyRate*30) using the UNROUNDED
            // annuity EMI; a whole-rupee-rounded EMI would round 1 rupee differently here, so derive the unrounded EMI.
            // That derivation assumes a plain annuity: no EMI override, no rounding-to-multiples, no principal grace.
            if (loanApplicationTerms.getFixedEmiAmount() != null
                    || (loanApplicationTerms.getInstallmentAmountInMultiplesOf() != null
                            && loanApplicationTerms.getInstallmentAmountInMultiplesOf() > 1)
                    || loanApplicationTerms.getPrincipalGrace() > 0) {
                log.warn(
                        "Broken-period first installment for product {} derives a plain-annuity EMI but the loan has "
                                + "fixedEmiAmount={}, installmentAmountInMultiplesOf={}, principalGrace={}; the derived "
                                + "principal/interest split may not match the level installment",
                        loanApplicationTerms.getLoanProductId(), loanApplicationTerms.getFixedEmiAmount(),
                        loanApplicationTerms.getInstallmentAmountInMultiplesOf(), loanApplicationTerms.getPrincipalGrace());
            }
            final BigDecimal monthlyRate = loanApplicationTerms.getAnnualNominalInterestRate().divide(MONTHLY_RATE_DIVISOR, mc);
            final int n = loanApplicationTerms.getNumberOfRepayments();
            final BigDecimal pow = BigDecimal.ONE.add(monthlyRate).pow(n, mc); // (1+r)^n
            final BigDecimal denominator = BigDecimal.ONE.subtract(BigDecimal.ONE.divide(pow, mc)); // 1 - (1+r)^-n
            final BigDecimal emiUnrounded = outstandingBalance.getAmount().multiply(monthlyRate, mc).divide(denominator, mc);
            final BigDecimal thirtyDayInterest = outstandingBalance.getAmount().multiply(monthlyRate, mc); // OB * r
            newPrincipal = Money.of(outstandingBalance.getCurrency(), emiUnrounded.subtract(thirtyDayInterest), mc);
        } else {
            // Short first period: installment stays = EMI; the interest saved moves to principal.
            newPrincipal = emi.minus(brokenInterest);
        }

        final PrincipalInterest result = new PrincipalInterest(newPrincipal, brokenInterest, stock.interestPaymentDueToGrace());
        result.setRescheduleInterestPortion(stock.getRescheduleInterestPortion());
        return result;
    }
}
